#!/bin/bash
# deploy_prod.sh
# Production deployment helper script for Ambient Expense Agent using Agent Runtime.

set -e

PROJECT_ID="gen-lang-client-0193227087"
REGION="us-east1"
PROJECT_NAME="ambient-expense-agent"

echo "========================================================="
echo "Starting Production Deployment for Ambient Expense Agent"
echo "========================================================="
echo "Target GCP Project: $PROJECT_ID"
echo "Target GCP Region:  $REGION"
echo ""

# 1. Verify gcloud authentication and project configuration
echo "[1/4] Verifying Google Cloud configuration..."
ACTIVE_ACCOUNT=$(gcloud config get-value account 2>/dev/null)
if [ -z "$ACTIVE_ACCOUNT" ]; then
    echo "Error: No active Google Cloud account found. Please run 'gcloud auth login' and try again."
    exit 1
fi
echo "Active account: $ACTIVE_ACCOUNT"

ACTIVE_PROJECT=$(gcloud config get-value project 2>/dev/null)
if [ "$ACTIVE_PROJECT" != "$PROJECT_ID" ]; then
    echo "Warning: Current gcloud project is '$ACTIVE_PROJECT', but target is '$PROJECT_ID'."
    read -p "Would you like to switch to '$PROJECT_ID'? (y/N): " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        gcloud config set project "$PROJECT_ID"
        echo "Switched project to $PROJECT_ID"
    else
        echo "Aborting deployment to avoid deploying to wrong project."
        exit 1
    fi
else
    echo "gcloud project is correctly set to $PROJECT_ID"
fi

# 2. Check if Terraform is installed for infra provisioning
echo ""
echo "[2/4] Checking Infrastructure (Terraform)..."
APP_SERVICE_ACCOUNT=""
LOGS_BUCKET_NAME=""

if command -v terraform &> /dev/null; then
    echo "Terraform detected. Initializing and checking production infrastructure..."
    pushd deployment/terraform/single-project > /dev/null
    
    # Initialize Terraform
    terraform init -reconfigure -input=false
    
    # Plan changes using production variables
    echo "Generating Terraform plan for production..."
    terraform plan -var-file="vars/prod.tfvars"
    
    read -p "Would you like to apply this Terraform plan to production? (y/N): " apply_confirm
    if [[ "$apply_confirm" =~ ^[Yy]$ ]]; then
        terraform apply -var-file="vars/prod.tfvars" -auto-approve
        
        # Fetch Terraform outputs
        APP_SERVICE_ACCOUNT=$(terraform output -raw app_service_account_email 2>/dev/null)
        LOGS_BUCKET_NAME=$(terraform output -raw logs_bucket_name 2>/dev/null)
        echo "Terraform applied successfully."
    else
        echo "Skipping Terraform application. Proceeding with existing infrastructure..."
    fi
    popd > /dev/null
else
    echo "Terraform is not installed or not in PATH. Skipping infrastructure provisioning."
    echo "You can still deploy using smart defaults or pre-created resources."
fi

# 3. Verify agents-cli installation
echo ""
echo "[3/4] Verifying agents-cli installation..."
if ! command -v agents-cli &> /dev/null; then
    echo "Error: agents-cli not found. Please install it using 'uv tool install google-agents-cli' before running this script."
    exit 1
fi
echo "agents-cli is available."

# 4. Deploying the Agent
echo ""
echo "[4/4] Deploying Agent to Agent Runtime..."

DEPLOY_CMD="agents-cli deploy --project $PROJECT_ID --region $REGION"

if [ -n "$APP_SERVICE_ACCOUNT" ]; then
    echo "Deploying with custom service account: $APP_SERVICE_ACCOUNT"
    DEPLOY_CMD="$DEPLOY_CMD --service-account $APP_SERVICE_ACCOUNT"
fi

if [ -n "$LOGS_BUCKET_NAME" ]; then
    echo "Configuring logs bucket env var: $LOGS_BUCKET_NAME"
    DEPLOY_CMD="$DEPLOY_CMD --update-env-vars LOGS_BUCKET_NAME=$LOGS_BUCKET_NAME"
fi

echo "Executing: $DEPLOY_CMD"
eval "$DEPLOY_CMD"

echo ""
echo "========================================================="
echo "Ambient Expense Agent successfully deployed to production!"
echo "========================================================="
