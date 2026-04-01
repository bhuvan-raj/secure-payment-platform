#!/usr/bin/env bash
# =============================================================================
#  bootstrap.sh — One-shot setup for Secure Payment Platform
#  Run: chmod +x scripts/bootstrap.sh && ./scripts/bootstrap.sh
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Prerequisites check ────────────────────────────────────────────────────────
check_prereqs() {
  info "Checking prerequisites..."
  local missing=()
  for cmd in aws terraform kubectl helm argocd docker git; do
    if ! command -v "$cmd" &>/dev/null; then
      missing+=("$cmd")
    fi
  done

  if [ ${#missing[@]} -gt 0 ]; then
    error "Missing required tools: ${missing[*]}\nInstall them and re-run."
  fi

  # Check AWS auth
  if ! aws sts get-caller-identity &>/dev/null; then
    error "AWS credentials not configured. Run: aws configure"
  fi
  success "All prerequisites satisfied"
}

# ── Variables ──────────────────────────────────────────────────────────────────
export AWS_REGION="${AWS_REGION:-us-east-1}"
export ENVIRONMENT="${ENVIRONMENT:-dev}"
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export CLUSTER_NAME="secure-payment-${ENVIRONMENT}"
export TF_DIR="terraform/environments/${ENVIRONMENT}"

info "AWS Account: ${AWS_ACCOUNT_ID}"
info "Region:      ${AWS_REGION}"
info "Environment: ${ENVIRONMENT}"
info "Cluster:     ${CLUSTER_NAME}"

# ── Step 1: Create Terraform state backend ─────────────────────────────────────
setup_tf_backend() {
  info "Setting up Terraform state backend..."
  local BUCKET_NAME="secure-payment-tfstate-${ENVIRONMENT}"
  local TABLE_NAME="terraform-state-lock"

  # S3 bucket
  if aws s3api head-bucket --bucket "${BUCKET_NAME}" 2>/dev/null; then
    warn "S3 bucket ${BUCKET_NAME} already exists — skipping"
  else
    aws s3api create-bucket \
      --bucket "${BUCKET_NAME}" \
      --region "${AWS_REGION}" \
      $([ "${AWS_REGION}" != "us-east-1" ] && echo "--create-bucket-configuration LocationConstraint=${AWS_REGION}" || true)

    aws s3api put-bucket-versioning \
      --bucket "${BUCKET_NAME}" \
      --versioning-configuration Status=Enabled

    aws s3api put-bucket-encryption \
      --bucket "${BUCKET_NAME}" \
      --server-side-encryption-configuration '{
        "Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"}}]
      }'

    aws s3api put-public-access-block \
      --bucket "${BUCKET_NAME}" \
      --public-access-block-configuration \
        BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

    success "S3 backend bucket created: ${BUCKET_NAME}"
  fi

  # DynamoDB lock table
  if aws dynamodb describe-table --table-name "${TABLE_NAME}" 2>/dev/null; then
    warn "DynamoDB table ${TABLE_NAME} already exists — skipping"
  else
    aws dynamodb create-table \
      --table-name "${TABLE_NAME}" \
      --attribute-definitions AttributeName=LockID,AttributeType=S \
      --key-schema AttributeName=LockID,KeyType=HASH \
      --billing-mode PAY_PER_REQUEST \
      --region "${AWS_REGION}"
    success "DynamoDB lock table created"
  fi
}

# ── Step 2: Create ECR repositories ───────────────────────────────────────────
setup_ecr() {
  info "Creating ECR repositories..."
  for repo in auth-service payment-service transaction-service; do
    if aws ecr describe-repositories --repository-names "${repo}" 2>/dev/null; then
      warn "ECR repo ${repo} already exists — skipping"
    else
      aws ecr create-repository \
        --repository-name "${repo}" \
        --image-scanning-configuration scanOnPush=true \
        --encryption-configuration encryptionType=KMS \
        --region "${AWS_REGION}"

      # Lifecycle policy — keep last 20 images
      aws ecr put-lifecycle-policy \
        --repository-name "${repo}" \
        --lifecycle-policy-text '{
          "rules":[{
            "rulePriority":1,
            "description":"Keep last 20 images",
            "selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":20},
            "action":{"type":"expire"}
          }]
        }'

      success "ECR repo created: ${repo}"
    fi
  done
}

# ── Step 3: Terraform apply ────────────────────────────────────────────────────
run_terraform() {
  info "Running Terraform..."
  pushd "${TF_DIR}" > /dev/null

  terraform init \
    -backend-config="bucket=secure-payment-tfstate-${ENVIRONMENT}" \
    -backend-config="key=${ENVIRONMENT}/terraform.tfstate" \
    -backend-config="region=${AWS_REGION}" \
    -backend-config="dynamodb_table=terraform-state-lock" \
    -backend-config="encrypt=true"

  terraform validate
  terraform plan -var="environment=${ENVIRONMENT}" -out=tfplan
  terraform apply -auto-approve tfplan
  success "Terraform applied successfully"
  popd > /dev/null
}

# ── Step 4: Configure kubectl ──────────────────────────────────────────────────
configure_kubectl() {
  info "Configuring kubectl..."
  aws eks update-kubeconfig \
    --name "${CLUSTER_NAME}" \
    --region "${AWS_REGION}"

  kubectl cluster-info
  success "kubectl configured"
}

# ── Step 5: Install AWS Load Balancer Controller ──────────────────────────────
install_alb_controller() {
  info "Installing AWS Load Balancer Controller..."
  helm repo add eks https://aws.github.io/eks-charts
  helm repo update

  helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
    -n kube-system \
    --set clusterName="${CLUSTER_NAME}" \
    --set serviceAccount.create=true \
    --set region="${AWS_REGION}" \
    --set vpcId="$(aws eks describe-cluster --name "${CLUSTER_NAME}" --query 'cluster.resourcesVpcConfig.vpcId' --output text)" \
    --wait

  success "ALB Controller installed"
}

# ── Step 6: Install ArgoCD ────────────────────────────────────────────────────
install_argocd() {
  info "Installing ArgoCD..."
  kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -

  kubectl apply -n argocd \
    -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

  # Wait for ArgoCD to be ready
  kubectl wait --for=condition=available deployment/argocd-server \
    -n argocd --timeout=120s

  # Apply project and applications
  kubectl apply -f argocd/projects/
  kubectl apply -f argocd/apps/

  ARGOCD_PASSWORD=$(kubectl -n argocd get secret argocd-initial-admin-secret \
    -o jsonpath="{.data.password}" | base64 -d)

  success "ArgoCD installed"
  info "ArgoCD admin password: ${ARGOCD_PASSWORD}"
  info "Port-forward: kubectl port-forward svc/argocd-server -n argocd 8080:443"
}

# ── Step 7: Build & push initial images ───────────────────────────────────────
build_and_push_images() {
  info "Building and pushing Docker images..."
  aws ecr get-login-password --region "${AWS_REGION}" | \
    docker login --username AWS --password-stdin \
    "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

  for svc in auth-service payment-service transaction-service; do
    local IMAGE="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${svc}"
    info "Building ${svc}..."
    docker build -t "${IMAGE}:latest" "services/${svc}/"
    docker push "${IMAGE}:latest"
    success "Pushed ${svc}"
  done
}

# ── Step 8: Apply K8s manifests ───────────────────────────────────────────────
apply_k8s_manifests() {
  info "Applying Kubernetes manifests..."

  # Replace ACCOUNT_ID placeholder
  find k8s/ -name "*.yaml" -exec \
    sed -i "s/ACCOUNT_ID/${AWS_ACCOUNT_ID}/g" {} \;

  kubectl apply -f k8s/base/namespace.yaml
  kubectl apply -f k8s/base/network-policy.yaml
  kubectl apply -k k8s/overlays/${ENVIRONMENT}/
  kubectl apply -f k8s/base/monitoring/

  success "Kubernetes manifests applied"
}

# ── Step 9: Verify deployment ─────────────────────────────────────────────────
verify_deployment() {
  info "Verifying deployment..."
  kubectl rollout status deployment/auth-service -n payment-platform --timeout=120s
  kubectl rollout status deployment/payment-service -n payment-platform --timeout=120s
  kubectl rollout status deployment/transaction-service -n payment-platform --timeout=120s

  echo ""
  info "=== Pod Status ==="
  kubectl get pods -n payment-platform
  echo ""
  info "=== Services ==="
  kubectl get svc -n payment-platform
  echo ""
  info "=== Ingress ==="
  kubectl get ingress -n payment-platform

  success "Deployment verified!"
}

# ── Main ───────────────────────────────────────────────────────────────────────
main() {
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Secure Payment Platform — Bootstrap"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""

  check_prereqs
  setup_tf_backend
  setup_ecr
  run_terraform
  configure_kubectl
  install_alb_controller
  install_argocd
  build_and_push_images
  apply_k8s_manifests
  verify_deployment

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  success "Bootstrap complete! Platform is live."
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

main "$@"
