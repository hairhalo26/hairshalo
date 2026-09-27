#!/usr/bin/env bash
# Create the S3 bucket for Hairshalo's product photographs, from AWS CloudShell.
#
# Paste this into CloudShell (region: ap-south-1) AFTER cloudshell-provision.sh
# has run. It creates or updates:
#   * an S3 bucket             hairshalo-products-<account-id>
#       - products/* publicly readable (the storefront loads photos from it)
#       - nothing else in it readable; no ACLs; encrypted at rest
#   * an inline role policy    product-media-write, on hairshalo-backup-role
#       - s3:PutObject and s3:DeleteObject on products/* only
#
# Only product photographs and videos go here. Every other image stays a plain
# file on the instance (MEDIA_DIR); the backup bucket is untouched and stays
# private.
#
# It is idempotent: re-running it changes nothing that is already right.
# Storage and transfer are billed per use (a few hundred product photos cost
# well under $1 a month to store).
set -euo pipefail

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
# The account this is allowed to build in (see cloudshell-provision.sh).
# Find it with:  aws sts get-caller-identity --query Account --output text
EXPECTED_ACCOUNT=""            # e.g. EXPECTED_ACCOUNT="123456789012"

REGION="ap-south-1"
NAME="hairshalo"
# ---------------------------------------------------------------------------

export AWS_DEFAULT_REGION="$REGION"
export AWS_PAGER=""

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
if [ -n "$EXPECTED_ACCOUNT" ] && [ "$ACCOUNT_ID" != "$EXPECTED_ACCOUNT" ]; then
  echo "Signed in to account $ACCOUNT_ID, but EXPECTED_ACCOUNT is $EXPECTED_ACCOUNT. Stopping." >&2
  exit 1
fi

BUCKET="${NAME}-products-${ACCOUNT_ID}"
ROLE="${NAME}-backup-role"

# ---------------------------------------------------------------------------
# 1. The instance role must already exist (cloudshell-provision.sh makes it)
# ---------------------------------------------------------------------------
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  echo "IAM role $ROLE not found. Run cloudshell-provision.sh first." >&2
  exit 1
fi

# An account-wide public access block overrides any bucket policy: the bucket
# would be created, but every photo would return 403 on the storefront.
ACCT_BLOCK="$(aws s3control get-public-access-block --account-id "$ACCOUNT_ID" \
  --query 'PublicAccessBlockConfiguration.[BlockPublicPolicy,RestrictPublicBuckets]' \
  --output text 2>/dev/null || echo "False False")"
case "$ACCT_BLOCK" in
  *True*)
    cat >&2 <<ERR

  This account blocks public bucket policies account-wide
  (S3 console -> "Block Public Access settings for this account").

  Product photos must be publicly readable for the storefront to show them.
  Turn off "Block public access to buckets and objects granted through new
  public bucket policies" and "...through any public bucket policies" for the
  ACCOUNT (the other two can stay on), then run this again. Each bucket keeps
  its own block: the backup bucket stays fully private.

ERR
    exit 1 ;;
esac

# ---------------------------------------------------------------------------
# 2. Bucket
# ---------------------------------------------------------------------------
say "Product photo bucket $BUCKET"
if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  info "exists"
else
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  info "created"
fi

# ACLs off: access is decided by the bucket policy alone.
aws s3api put-bucket-ownership-controls --bucket "$BUCKET" \
  --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
# Block ACL-based public access, allow a public bucket POLICY (below).
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false"
info "ACLs disabled, encrypted, public access only through the policy"

# Anyone may READ products/*. Nobody outside the account may list the bucket,
# write to it, or read anything outside products/.
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[{
    \"Sid\":\"PublicReadProductMedia\",
    \"Effect\":\"Allow\",
    \"Principal\":\"*\",
    \"Action\":\"s3:GetObject\",
    \"Resource\":\"arn:aws:s3:::${BUCKET}/products/*\"
  }]
}"
info "policy: public read on products/* only"

# ---------------------------------------------------------------------------
# 3. Let the instance write and delete product photos, nothing more
# ---------------------------------------------------------------------------
say "IAM policy product-media-write on $ROLE"
aws iam put-role-policy --role-name "$ROLE" --policy-name "product-media-write" \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Action\":[\"s3:PutObject\",\"s3:DeleteObject\"],
      \"Resource\":\"arn:aws:s3:::${BUCKET}/products/*\"
    }]
  }"
info "PutObject + DeleteObject on products/* only"

cat <<DONE

  Done. Add these to .env.prod on the instance:

       PRODUCT_MEDIA_STORAGE=s3
       PRODUCT_MEDIA_BUCKET=$BUCKET
       PRODUCT_MEDIA_REGION=$REGION

  Then follow "Images" in docs/DEPLOY-AWS.md to move the existing files.

DONE
