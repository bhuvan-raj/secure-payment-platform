# 🔐 Secure Cloud-Native Payment Platform

> **Production-grade DevSecOps on AWS** — EKS · Terraform · ArgoCD · GitHub Actions · Prometheus · Loki

---

## Architecture Overview

```
Internet
   │
   ▼
AWS WAF (rate limiting, SQLi/XSS protection)
   │
   ▼
Application Load Balancer (HTTPS only, TLS 1.2+)
   │
   ├─── /v1/auth/*     ──► Auth Service (Pod)
   └─── /v1/payments/* ──► Payment Service (Pod)
                                  │
                            SQS FIFO Queue
                                  │
                            Transaction Service (Pod)
                                  │
                            RDS PostgreSQL (encrypted, Multi-AZ in prod)

All pods run in private subnets.
AWS Secrets Manager provides credentials at runtime (no hardcoded secrets).
IRSA (IAM Roles for Service Accounts) provides pod-level AWS access.
```

---

## Project Structure

```
secure-payment-platform/
├── services/
│   ├── auth-service/          # JWT authentication (Flask)
│   ├── payment-service/       # Payment initiation (Flask)
│   └── transaction-service/   # Ledger processor (Python, SQS consumer)
├── terraform/
│   ├── modules/
│   │   ├── vpc/               # VPC, subnets, NAT gateways, flow logs
│   │   ├── eks/               # EKS cluster, node groups, OIDC, add-ons
│   │   ├── rds/               # PostgreSQL, Secrets Manager, monitoring
│   │   ├── kms/               # KMS keys per resource type
│   │   ├── iam/               # IRSA roles (least privilege per service)
│   │   └── security/          # WAF, GuardDuty, CloudTrail, SQS
│   └── environments/
│       ├── dev/               # Dev Terraform root
│       └── prod/              # Prod Terraform root
├── k8s/
│   ├── base/                  # Base Kubernetes manifests
│   │   ├── auth-service/
│   │   ├── payment-service/
│   │   ├── transaction-service/
│   │   ├── ingress/           # ALB Ingress
│   │   └── monitoring/        # Prometheus, Grafana, Loki, Alertmanager
│   └── overlays/
│       ├── dev/               # Kustomize patches for dev
│       └── prod/              # Kustomize patches for prod
├── argocd/
│   ├── apps/                  # ArgoCD Application CRDs
│   └── projects/              # ArgoCD AppProject with RBAC + sync windows
├── .github/workflows/
│   ├── ci-cd.yaml             # Main pipeline (SAST → Test → Build → Scan → GitOps)
│   └── terraform.yaml         # Infrastructure pipeline
└── scripts/
    └── bootstrap.sh           # One-command full environment setup
```

---

## Quick Start

### Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| AWS CLI | ≥ 2.15 | AWS API access |
| Terraform | ≥ 1.7 | Infrastructure |
| kubectl | ≥ 1.29 | Kubernetes |
| Helm | ≥ 3.14 | K8s package manager |
| Docker | ≥ 24 | Container builds |
| ArgoCD CLI | ≥ 2.10 | GitOps management |

### Bootstrap (full environment)

```bash
# Clone the repo
git clone https://github.com/YOUR_ORG/secure-payment-platform.git
cd secure-payment-platform

# Configure AWS credentials
aws configure

# One-command bootstrap (dev environment)
chmod +x scripts/bootstrap.sh
./scripts/bootstrap.sh

# Or target prod
ENVIRONMENT=prod ./scripts/bootstrap.sh
```

### Manual step-by-step

```bash
# 1. Create Terraform state backend
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws s3api create-bucket --bucket "secure-payment-tfstate-dev" --region us-east-1
aws s3api put-bucket-versioning --bucket "secure-payment-tfstate-dev" \
  --versioning-configuration Status=Enabled
aws dynamodb create-table --table-name terraform-state-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST

# 2. Provision infrastructure
cd terraform/environments/dev
terraform init
terraform plan -var="environment=dev"
terraform apply -var="environment=dev"

# 3. Configure kubectl
aws eks update-kubeconfig --name secure-payment-dev --region us-east-1

# 4. Install ALB Controller
helm repo add eks https://aws.github.io/eks-charts && helm repo update
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system --set clusterName=secure-payment-dev

# 5. Install ArgoCD
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl apply -f argocd/projects/
kubectl apply -f argocd/apps/

# 6. Build & push images
for svc in auth-service payment-service transaction-service; do
  aws ecr create-repository --repository-name $svc --image-scanning-configuration scanOnPush=true
  docker build -t $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/$svc:latest services/$svc/
  docker push $AWS_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/$svc:latest
done

# 7. Apply manifests
sed -i "s/ACCOUNT_ID/$AWS_ACCOUNT_ID/g" $(find k8s/ -name "*.yaml")
kubectl apply -f k8s/base/namespace.yaml
kubectl apply -k k8s/overlays/dev/
kubectl apply -f k8s/base/monitoring/
```

---

## Services & API Reference

### Auth Service (`port 8000`)

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/health` | GET | None | Liveness probe |
| `/ready` | GET | None | Readiness probe (DB check) |
| `/metrics` | GET | None | Prometheus metrics |
| `/v1/auth/register` | POST | None | Register new user |
| `/v1/auth/login` | POST | None | Login → JWT |
| `/v1/auth/verify` | POST | None | Validate JWT (internal) |
| `/v1/auth/me` | GET | Bearer JWT | Current user info |

### Payment Service (`port 8001`)

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/v1/payments` | POST | Bearer JWT | Create payment (async) |
| `/v1/payments` | GET | Bearer JWT | List payments (paginated) |
| `/v1/payments/:id` | GET | Bearer JWT | Get payment status |

### Transaction Service (`port 8002`)

- Internal SQS consumer — no public HTTP API
- Exposes Prometheus metrics on `/` (port 8002)

---

## Security Controls

| Control | Implementation |
|---------|---------------|
| No hardcoded secrets | AWS Secrets Manager + IRSA |
| Encryption at rest | KMS for EKS secrets, RDS, S3, SQS |
| Encryption in transit | TLS enforced (ALB, RDS `sslmode=require`) |
| Network isolation | NetworkPolicy (default-deny-all) |
| Pod security | Non-root, read-only filesystem, dropped capabilities, seccomp |
| IMDSv2 enforced | Launch template `http_tokens=required` |
| Container scanning | Trivy on every build (CRITICAL/HIGH = fail) |
| SAST | Bandit (Python), Safety (dependencies) |
| IaC scanning | tfsec, checkov in CI |
| WAF | AWS WAF v2 (rate limiting, OWASP rules, SQLi) |
| Threat detection | AWS GuardDuty (EKS, S3, EC2 malware) |
| Audit logging | CloudTrail (multi-region, log file validation) |
| VPC Flow Logs | All traffic logged to CloudWatch |
| Supply chain | Cosign image signing (keyless, sigstore) |
| CI credentials | GitHub OIDC → IAM role (no static AWS keys) |

---

## Observability

### Access Dashboards

```bash
# Prometheus
kubectl port-forward svc/prometheus -n monitoring 9090:9090
# Open: http://localhost:9090

# Grafana  (admin / from grafana-secret)
kubectl port-forward svc/grafana -n monitoring 3000:3000
# Open: http://localhost:3000

# ArgoCD  (admin / from argocd-initial-admin-secret)
kubectl port-forward svc/argocd-server -n argocd 8080:443
# Open: https://localhost:8080
```

### Key Alerts

| Alert | Threshold | Severity |
|-------|-----------|----------|
| High payment error rate | >5% over 5m | CRITICAL |
| Payment/Auth service down | 0 pods up | CRITICAL |
| High login failure rate | >30% over 5m | WARNING (possible brute-force) |
| SQS queue depth | >1000 msgs | WARNING |
| API P95 latency | >1s | WARNING |
| Pod memory usage | >85% of limit | WARNING |

---

## GitOps Workflow

```
Developer pushes code
       │
       ▼
GitHub Actions CI pipeline:
  1. Bandit SAST + Safety scan
  2. Unit tests (pytest)
  3. Docker build
  4. Trivy container scan  ← CRITICAL/HIGH = pipeline fails
  5. Push to ECR (main only)
  6. Cosign image signing
  7. Update image tag in k8s/base/*/deployment.yaml
  8. Git push (triggers ArgoCD sync)
       │
       ▼
ArgoCD detects git change
       │
       ▼
ArgoCD syncs to EKS cluster
  - Prune removed resources
  - Self-heal drift
  - Retry on failure (5x with backoff)
```

---

## Cost Estimates (dev environment)

| Resource | Approx Monthly Cost |
|----------|---------------------|
| EKS cluster | ~$73 |
| 2× t3.medium nodes | ~$60 |
| RDS t3.medium (single-AZ) | ~$60 |
| NAT Gateways (3×) | ~$100 |
| ALB | ~$20 |
| CloudTrail + S3 | ~$5 |
| GuardDuty | ~$5 |
| **Total (dev)** | **~$323/month** |

> 💡 **Cost tip**: Use 1 NAT gateway in dev (saves ~$67/month). Set `nat_gateway_count = 1` in dev variables.

---

## GitHub Secrets Required

Configure these in your GitHub repository settings:

```
AWS_ACCOUNT_ID          — Your 12-digit AWS account ID
GITOPS_TOKEN            — GitHub PAT with repo write access (for image tag updates)
SLACK_WEBHOOK_URL       — Slack incoming webhook for deploy notifications
```

GitHub OIDC is used for AWS authentication — **no static AWS keys needed**.

---

## Troubleshooting

```bash
# Check pod logs
kubectl logs -f deploy/auth-service -n payment-platform

# Check pod events
kubectl describe pod -l app=auth-service -n payment-platform

# Check IRSA is working (should show role ARN)
kubectl exec -it deploy/auth-service -n payment-platform -- \
  aws sts get-caller-identity

# Test Secrets Manager access from pod
kubectl exec -it deploy/auth-service -n payment-platform -- \
  aws secretsmanager get-secret-value --secret-id dev/payment-platform/db-auth

# ArgoCD sync status
argocd app list
argocd app sync auth-service

# Prometheus targets
kubectl port-forward svc/prometheus -n monitoring 9090:9090
# Visit: http://localhost:9090/targets
```
