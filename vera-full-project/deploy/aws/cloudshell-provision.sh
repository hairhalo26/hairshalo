#!/usr/bin/env bash
# Provision the Hairshalo AWS infrastructure from AWS CloudShell.
#
# Paste this into CloudShell (region: ap-south-1). It creates:
#   * a security group        hairshalo-sg
#   * an ED25519 key pair     hairshalo
#   * an S3 backup bucket     hairshalo-backups-<account-id>
#   * an IAM role + profile   hairshalo-backup-role
#   * a t3.small instance     hairshalo-prod  (Ubuntu 24.04, 30 GB gp3, encrypted)
#                             override with INSTANCE_TYPE=... for a bigger box
#   * an Elastic IP, associated with that instance
#
# It is idempotent: every resource is checked for first, so re-running after a
# failure continues rather than duplicating. It creates BILLABLE resources
# (~$32-40/month). deploy/aws/cloudshell-teardown.sh removes them again.
#
# It deliberately does NOT open SSH to the world. Set MY_IP below first.
set -euo pipefail

# ---------------------------------------------------------------------------
# CONFIG - edit MY_IP, then paste the whole script.
# ---------------------------------------------------------------------------

# Your laptop's public IP, for the SSH rule. Get it by running this ON YOUR
# LAPTOP (not in CloudShell - CloudShell has its own address that changes):
#
#     curl -s https://checkip.amazonaws.com
#
MY_IP=""                       # e.g. MY_IP="203.0.113.45"

# The account this is allowed to build in. Leave empty to skip the check, but
# filling it in is the difference between a typo and a shop deployed into
# another business's account: CloudShell inherits whichever console session you
# happen to be signed into, and nothing on screen makes that obvious.
# Find it with:  aws sts get-caller-identity --query Account --output text
EXPECTED_ACCOUNT=""            # e.g. EXPECTED_ACCOUNT="123456789012"

REGION="ap-south-1"
# t3.small (2 vCPU, 2 GB) is what production runs on, and it is free-tier
# eligible -- accounts on the new AWS free plan may only launch eligible types,
# and RunInstances rejects anything else with "not eligible for Free Tier".
# c7i-flex.large (4 GB) is also eligible and is what the stack was originally
# sized for; it draws down plan credits several times faster. Override without
# editing this file:
#     INSTANCE_TYPE=c7i-flex.large bash provision.sh
# Whatever you pick, match the memory caps in .env.prod -- the sizing blocks
# are in .env.prod.example, and docs/DEPLOY-AWS.md explains the trade.
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.small}"
VOLUME_GB="30"
NAME="hairshalo"

# ---------------------------------------------------------------------------

export AWS_DEFAULT_REGION="$REGION"
export AWS_PAGER=""            # stop the CLI opening a pager on every output

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }

if [ -z "$MY_IP" ]; then
  cat >&2 <<'ERR'

  MY_IP is not set.

  Run this on your LAPTOP (not in CloudShell) to find your public address:

      curl -s https://checkip.amazonaws.com

  Then set it at the top of this script, e.g. MY_IP="203.0.113.45", and paste
  again. This is left blank on purpose: the alternative is an SSH port open to
  the whole internet, which collects thousands of login attempts a day.

ERR
  exit 1
fi

# Reject anything that is not a bare IPv4 address - a CIDR pasted here would
# silently widen the rule (a stray /0 opens SSH to everyone).
if ! printf '%s' "$MY_IP" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; then
  echo "MY_IP must be a plain IPv4 address with no /mask - got: $MY_IP" >&2
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${NAME}-backups-${ACCOUNT_ID}"

if [ -n "$EXPECTED_ACCOUNT" ] && [ "$ACCOUNT_ID" != "$EXPECTED_ACCOUNT" ]; then
  cat >&2 <<ERR

  WRONG ACCOUNT - nothing was created.

    expected  $EXPECTED_ACCOUNT
    signed in $ACCOUNT_ID  ($(aws sts get-caller-identity --query Arn --output text))

  Sign CloudShell into the Hairshalo account and paste again.

ERR
  exit 1
fi

say "Account $ACCOUNT_ID in $REGION"
info "$(aws sts get-caller-identity --query Arn --output text)"

# Existing instances here, so a shared account is visible before anything is
# built rather than discovered afterwards.
RUNNING="$(aws ec2 describe-instances \
  --filters "Name=instance-state-name,Values=running" \
  --query 'length(Reservations[].Instances[])' --output text 2>/dev/null || echo 0)"
if [ "$RUNNING" != "0" ] && [ "$RUNNING" != "None" ]; then
  info "note: $RUNNING instance(s) already running in this region"
fi

# ---------------------------------------------------------------------------
# 1. Default VPC
# ---------------------------------------------------------------------------
say "Finding the default VPC"
VPC_ID="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)"

if [ "$VPC_ID" = "None" ] || [ -z "$VPC_ID" ]; then
  echo "No default VPC in $REGION. Create one with:" >&2
  echo "    aws ec2 create-default-vpc --region $REGION" >&2
  exit 1
fi
info "VPC: $VPC_ID"

SUBNET_ID="$(aws ec2 describe-subnets \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=default-for-az,Values=true" \
  --query 'Subnets[0].SubnetId' --output text)"
info "Subnet: $SUBNET_ID"

# ---------------------------------------------------------------------------
# 2. Security group
# ---------------------------------------------------------------------------
say "Security group ${NAME}-sg"
SG_ID="$(aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=${NAME}-sg" "Name=vpc-id,Values=$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo None)"

if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
  SG_ID="$(aws ec2 create-security-group \
    --group-name "${NAME}-sg" \
    --description "Hairshalo production: web in, SSH from one address" \
    --vpc-id "$VPC_ID" \
    --query GroupId --output text)"
  info "created $SG_ID"
else
  info "exists: $SG_ID"
fi

# Rules are added individually and failures ignored, so re-running does not
# abort on "already exists". Postgres (5432) is deliberately absent: it
# publishes no host port and only the other containers reach it.
add_rule() {  # port  cidr  description
  aws ec2 authorize-security-group-ingress \
    --group-id "$SG_ID" \
    --ip-permissions "IpProtocol=tcp,FromPort=$1,ToPort=$1,IpRanges=[{CidrIp=$2,Description=$3}]" \
    >/dev/null 2>&1 && info "opened $1 to $2" || info "$1 to $2 already present"
}
add_rule 22  "${MY_IP}/32" "ssh-from-operator"
add_rule 80  "0.0.0.0/0"   "http-acme-and-redirect"
add_rule 443 "0.0.0.0/0"   "https"

# ---------------------------------------------------------------------------
# 3. Key pair
# ---------------------------------------------------------------------------
say "Key pair $NAME"
if aws ec2 describe-key-pairs --key-names "$NAME" >/dev/null 2>&1; then
  info "exists already - reusing it"
  info "AWS cannot re-issue a private key. If you no longer have ${NAME}.pem,"
  info "deleting this key pair and re-running does NOT regain access to an"
  info "instance already built with it -- the old public key stays in that"
  info "instance's authorized_keys. Add a new public key over a session you"
  info "still have, or via EC2 Instance Connect; failing both, terminate the"
  info "instance and re-run from a fresh key pair."
else
  aws ec2 create-key-pair --key-name "$NAME" --key-type ed25519 \
    --query KeyMaterial --output text > "${HOME}/${NAME}.pem"
  chmod 400 "${HOME}/${NAME}.pem"
  info "created ${HOME}/${NAME}.pem"
fi

# ---------------------------------------------------------------------------
# 4. S3 backup bucket
# ---------------------------------------------------------------------------
say "Backup bucket $BUCKET"
if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  info "exists already"
else
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  info "created"
fi

aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
# Versioning turns an accidental overwrite into something recoverable.
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
info "public access blocked, versioning + encryption on"

# ---------------------------------------------------------------------------
# 5. IAM role so the instance can upload backups without stored keys
# ---------------------------------------------------------------------------
say "IAM role ${NAME}-backup-role"
ROLE="${NAME}-backup-role"
PROFILE="${NAME}-backup-profile"

if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" \
    --description "Lets the Hairshalo instance upload database backups" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]
    }' >/dev/null
  info "created role"
else
  info "role exists"
fi

# PutObject and ListBucket, but NOT DeleteObject: a compromised instance must
# not be able to erase the backup history it has been writing.
aws iam put-role-policy --role-name "$ROLE" --policy-name "backup-write" \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Action\":[\"s3:PutObject\",\"s3:ListBucket\"],
      \"Resource\":[\"arn:aws:s3:::${BUCKET}\",\"arn:aws:s3:::${BUCKET}/*\"]
    }]
  }"
info "policy attached (PutObject + ListBucket only)"

if ! aws iam get-instance-profile --instance-profile-name "$PROFILE" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$PROFILE" >/dev/null
  aws iam add-role-to-instance-profile \
    --instance-profile-name "$PROFILE" --role-name "$ROLE" >/dev/null
  info "created instance profile"
else
  info "instance profile exists"
fi

# ---------------------------------------------------------------------------
# 6. Ubuntu 24.04 AMI
# ---------------------------------------------------------------------------
say "Latest Ubuntu 24.04 AMI"
AMI_ID="$(aws ssm get-parameters \
  --names /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query 'Parameters[0].Value' --output text 2>/dev/null || echo "")"

if [ -z "$AMI_ID" ] || [ "$AMI_ID" = "None" ]; then
  info "SSM parameter unavailable, querying Canonical's images directly"
  AMI_ID="$(aws ec2 describe-images --owners 099720109477 \
    --filters 'Name=name,Values=ubuntu/images/hvm-ssd*/ubuntu-noble-24.04-amd64-server-*' \
              'Name=state,Values=available' \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)"
fi
[ -n "$AMI_ID" ] && [ "$AMI_ID" != "None" ] || { echo "Could not resolve an AMI" >&2; exit 1; }
info "AMI: $AMI_ID"

# ---------------------------------------------------------------------------
# 7. The instance
# ---------------------------------------------------------------------------
say "Instance ${NAME}-prod"
INSTANCE_ID="$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=${NAME}-prod" \
            "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || echo None)"

if [ "$INSTANCE_ID" = "None" ] || [ -z "$INSTANCE_ID" ]; then
  # IAM is eventually consistent: a profile created seconds ago is often not
  # yet visible to RunInstances, which fails with "Invalid IAM Instance
  # Profile name". That one is worth retrying. Everything else -- a quota of
  # zero, an account still being verified, a bad AMI -- will fail identically
  # on every attempt, so retrying it just hides the reason behind a wall of
  # identical lines. Keep the error and decide.
  ERR_LOG="$(mktemp)"
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if INSTANCE_ID="$(aws ec2 run-instances \
        --image-id "$AMI_ID" \
        --instance-type "$INSTANCE_TYPE" \
        --key-name "$NAME" \
        --security-group-ids "$SG_ID" \
        --subnet-id "$SUBNET_ID" \
        --associate-public-ip-address \
        --iam-instance-profile "Name=$PROFILE" \
        --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
        --block-device-mappings "[{\"DeviceName\":\"/dev/sda1\",\"Ebs\":{\"VolumeSize\":${VOLUME_GB},\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true,\"Encrypted\":true}}]" \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${NAME}-prod},{Key=Project,Value=hairshalo}]" \
        --query 'Instances[0].InstanceId' --output text 2>"$ERR_LOG")"; then
      break
    fi
    INSTANCE_ID=""

    # Only the IAM propagation delay is transient. Anything else: stop now and
    # show what AWS actually said.
    if ! grep -qiE 'Invalid IAM Instance Profile|iamInstanceProfile' "$ERR_LOG"; then
      echo >&2
      echo "  RunInstances failed, and not for a reason that retrying fixes:" >&2
      echo >&2
      sed 's/^/    /' "$ERR_LOG" >&2
      echo >&2
      case "$(cat "$ERR_LOG")" in
        *VcpuLimitExceeded*|*InstanceLimitExceeded*)
          echo "  Your EC2 vCPU quota is too low for a $INSTANCE_TYPE." >&2
          echo "  Request an increase: Service Quotas -> EC2 -> Running On-Demand" >&2
          echo "  Standard instances. New accounts are sometimes capped at 0." >&2 ;;
        *PendingVerification*|*not\ been\ verified*|*OptInRequired*)
          echo "  The account is still being verified. This clears by itself," >&2
          echo "  usually within a couple of hours. Re-run then." >&2 ;;
        *"not eligible for Free Tier"*|*InvalidParameterCombination*)
          echo "  This account may only launch free-tier-eligible instance" >&2
          echo "  types. Either upgrade it to a paid plan in the Billing" >&2
          echo "  console, or re-run with a free-tier type:" >&2
          echo >&2
          echo "    aws ec2 describe-instance-types \\" >&2
          echo "      --filters Name=free-tier-eligible,Values=true \\" >&2
          echo "      --query 'InstanceTypes[].InstanceType' --output text" >&2
          echo >&2
          echo "    INSTANCE_TYPE=t3.small bash $0" >&2 ;;
        *UnauthorizedOperation*)
          echo "  This identity may not launch instances. Use an admin user." >&2 ;;
      esac
      rm -f "$ERR_LOG"
      exit 1
    fi

    info "IAM profile not visible yet (attempt $attempt) - waiting 5s"
    sleep 5
  done
  if [ -z "$INSTANCE_ID" ]; then
    echo "  RunInstances still refused after 10 attempts. Last error:" >&2
    sed 's/^/    /' "$ERR_LOG" >&2
    rm -f "$ERR_LOG"
    exit 1
  fi
  rm -f "$ERR_LOG"
  info "launched $INSTANCE_ID"
else
  info "exists: $INSTANCE_ID"
fi

say "Waiting for the instance to reach running"
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
info "running"

# ---------------------------------------------------------------------------
# 8. Elastic IP
# ---------------------------------------------------------------------------
# A default public IP changes every time the instance stops and starts, which
# would silently break DNS one morning.
say "Elastic IP"
ALLOC_ID="$(aws ec2 describe-addresses \
  --filters "Name=tag:Name,Values=${NAME}-eip" \
  --query 'Addresses[0].AllocationId' --output text 2>/dev/null || echo None)"

if [ "$ALLOC_ID" = "None" ] || [ -z "$ALLOC_ID" ]; then
  ALLOC_ID="$(aws ec2 allocate-address --domain vpc \
    --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=${NAME}-eip}]" \
    --query AllocationId --output text)"
  info "allocated $ALLOC_ID"
else
  info "exists: $ALLOC_ID"
fi

aws ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$ALLOC_ID" >/dev/null
ELASTIC_IP="$(aws ec2 describe-addresses --allocation-ids "$ALLOC_ID" \
  --query 'Addresses[0].PublicIp' --output text)"
info "associated with $INSTANCE_ID"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
cat <<SUMMARY

===========================================================================
  DONE

  Elastic IP     $ELASTIC_IP        <- both GoDaddy A records point here
  Instance       $INSTANCE_ID
  Security group $SG_ID
  Backup bucket  $BUCKET
  Key file       ${HOME}/${NAME}.pem

  NEXT

  1. Download the private key out of CloudShell before you close it:
       CloudShell menu (top right) -> Actions -> Download file
       Path:  ${NAME}.pem
     Then on your laptop:  chmod 400 ~/Downloads/${NAME}.pem

  2. At GoDaddy, delete the parked records and add TWO A records,
     @ and www, both pointing at $ELASTIC_IP, TTL 600.
     Wait until both resolve before starting the stack - Caddy's first
     certificate request fails if they do not.

  3. Then SSH in and provision the host:
       ssh -i ~/Downloads/${NAME}.pem ubuntu@$ELASTIC_IP

  4. Put this in .env.prod on the instance:
       BACKUP_S3_BUCKET=$BUCKET

===========================================================================

SUMMARY
