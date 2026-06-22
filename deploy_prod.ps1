# deploy_prod.ps1
# Production deployment helper script for Ambient Expense Agent using Agent Runtime.

$ProjectID = "gen-lang-client-0193227087"
$Region = "us-east1"
$ProjectName = "ambient-expense-agent"

Write-Host "=========================================================" -ForegroundColor Cyan
Write-Host "Starting Production Deployment for Ambient Expense Agent" -ForegroundColor Cyan
Write-Host "=========================================================" -ForegroundColor Cyan
Write-Host "Target GCP Project: $ProjectID" -ForegroundColor Yellow
Write-Host "Target GCP Region:  $Region" -ForegroundColor Yellow
Write-Host ""

# 1. Verify gcloud authentication and project configuration
Write-Host "[1/4] Verifying Google Cloud configuration..." -ForegroundColor Blue
$ActiveAccount = gcloud config get-value account 2>$null
if (-not $ActiveAccount) {
    Write-Error "No active Google Cloud account found. Please run 'gcloud auth login' and try again."
    Exit 1
}
Write-Host "Active account: $ActiveAccount" -ForegroundColor Green

$ActiveProject = gcloud config get-value project 2>$null
if ($ActiveProject -ne $ProjectID) {
    Write-Host "Warning: Current gcloud project is '$ActiveProject', but target is '$ProjectID'." -ForegroundColor Yellow
    $Confirm = Read-Host "Would you like to switch to '$ProjectID'? (y/N)"
    if ($Confirm -eq 'y' -or $Confirm -eq 'Y') {
        gcloud config set project $ProjectID
        Write-Host "Switched project to $ProjectID" -ForegroundColor Green
    } else {
        Write-Host "Aborting deployment to avoid deploying to wrong project." -ForegroundColor Red
        Exit 1
    }
} else {
    Write-Host "gcloud project is correctly set to $ProjectID" -ForegroundColor Green
}

# 2. Check if Terraform is installed for infra provisioning
Write-Host ""
Write-Host "[2/4] Checking Infrastructure (Terraform)..." -ForegroundColor Blue
$HasTerraform = Get-Command terraform -ErrorAction SilentlyContinue
$AppServiceAccount = $null
$LogsBucketName = $null

if ($HasTerraform) {
    Write-Host "Terraform detected. Initializing and checking production infrastructure..." -ForegroundColor Green
    Push-Location deployment/terraform/single-project
    try {
        # Initialize Terraform
        terraform init -reconfigure -input=false
        
        # Plan changes using production variables
        Write-Host "Generating Terraform plan for production..." -ForegroundColor Blue
        terraform plan -var-file="vars/prod.tfvars"
        
        $ApplyConfirm = Read-Host "Would you like to apply this Terraform plan to production? (y/N)"
        if ($ApplyConfirm -eq 'y' -or $ApplyConfirm -eq 'Y') {
            terraform apply -var-file="vars/prod.tfvars" -auto-approve
            
            # Fetch Terraform outputs
            $AppServiceAccount = terraform output -raw app_service_account_email 2>$null
            $LogsBucketName = terraform output -raw logs_bucket_name 2>$null
            Write-Host "Terraform applied successfully." -ForegroundColor Green
        } else {
            Write-Host "Skipping Terraform application. Proceeding with existing infrastructure..." -ForegroundColor Yellow
        }
    }
    catch {
        Write-Warning "Terraform execution failed. Will attempt direct deployment using default credentials."
    }
    Pop-Location
} else {
    Write-Host "Terraform is not installed or not in PATH. Skipping infrastructure provisioning." -ForegroundColor Yellow
    Write-Host "You can still deploy using smart defaults or pre-created resources." -ForegroundColor Yellow
}

# 3. Verify agents-cli installation
Write-Host ""
Write-Host "[3/4] Verifying agents-cli installation..." -ForegroundColor Blue
$HasAgentsCli = Get-Command agents-cli -ErrorAction SilentlyContinue
if (-not $HasAgentsCli) {
    Write-Error "agents-cli not found. Please install it using 'uv tool install google-agents-cli' before running this script."
    Exit 1
}
Write-Host "agents-cli is available." -ForegroundColor Green

# 4. Deploying the Agent
Write-Host ""
Write-Host "[4/4] Deploying Agent to Agent Runtime..." -ForegroundColor Blue

$DeployCmd = "agents-cli deploy --project $ProjectID --region $Region"

if ($AppServiceAccount) {
    Write-Host "Deploying with custom service account: $AppServiceAccount" -ForegroundColor Green
    $DeployCmd += " --service-account $AppServiceAccount"
}

if ($LogsBucketName) {
    Write-Host "Configuring logs bucket env var: $LogsBucketName" -ForegroundColor Green
    $DeployCmd += " --update-env-vars LOGS_BUCKET_NAME=$LogsBucketName"
}

Write-Host "Executing: $DeployCmd" -ForegroundColor Cyan
Invoke-Expression $DeployCmd

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "=========================================================" -ForegroundColor Green
    Write-Host "Ambient Expense Agent successfully deployed to production!" -ForegroundColor Green
    Write-Host "=========================================================" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "Deployment failed. Please check the logs above." -ForegroundColor Red
    Exit $LASTEXITCODE
}
