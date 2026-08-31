#!/usr/bin/env bash
# Remove everything cloudshell-provision.sh created.
#
#   *** THIS DESTROYS THE INSTANCE AND ITS DATABASE. ***
#
# It exists so an abandoned trial does not quietly bill you every month, and
# so a botched first attempt can be cleanly restarted. It is not part of the
# deployment - do not run it against a live shop.
#
# The backup bucket is NOT deleted. Backups are the one thing worth keeping
# when everything else goes, and a script that erases them by default is a
# script that will one day erase them by accident. Remove it by hand if you
# genuinely want it gone.
set -euo pipefail

REGION="ap-south-1"
NAME="hairshalo"

export AWS_DEFAULT_REGION="$REGION"
export AWS_PAGER=""

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${NAME}-backups-${ACCOUNT_ID}"

INSTANCE_ID="$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=${NAME}-prod" \
            "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || echo None)"

cat <<WARN

  About to permanently delete, in account $ACCOUNT_ID ($REGION):

    instance         ${INSTANCE_ID}   <-- INCLUDING ITS DATABASE
    elastic IP       ${NAME}-eip
    security group   ${NAME}-sg
    key pair         ${NAME}
    IAM role         ${NAME}-backup-role
    instance profile ${NAME}-backup-profile

  KEPT: the backup bucket $BUCKET and everything in it.

WARN

printf 'Type DELETE to confirm: '
read -r CONFIRM
[ "$CONFIRM" = "DELETE" ] || { echo "Cancelled - nothing was changed."; exit 1; }

if [ "$INSTANCE_ID" != "None" ] && [ -n "$INSTANCE_ID" ]; then
  say "Terminating $INSTANCE_ID"
  aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" >/dev/null
  aws ec2 wait instance-terminated --instance-ids "$INSTANCE_ID"
  echo "    terminated"
fi

say "Releasing the Elastic IP"
ALLOC_ID="$(aws ec2 describe-addresses --filters "Name=tag:Name,Values=${NAME}-eip" \
  --query 'Addresses[0].AllocationId' --output text 2>/dev/null || echo None)"
if [ "$ALLOC_ID" != "None" ] && [ -n "$ALLOC_ID" ]; then
  aws ec2 release-address --allocation-id "$ALLOC_ID" && echo "    released"
fi

say "Deleting the security group"
# The group cannot go until the instance's network interface is really gone,
# which lags termination by a few seconds.
SG_ID="$(aws ec2 describe-security-groups --filters "Name=group-name,Values=${NAME}-sg" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo None)"
if [ "$SG_ID" != "None" ] && [ -n "$SG_ID" ]; then
  for attempt in 1 2 3 4 5 6; do
    if aws ec2 delete-security-group --group-id "$SG_ID" 2>/dev/null; then
      echo "    deleted"; break
    fi
    echo "    still in use (attempt $attempt) - waiting 10s"
    sleep 10
  done
fi

say "Deleting the key pair"
aws ec2 delete-key-pair --key-name "$NAME" >/dev/null 2>&1 && echo "    deleted" || true

say "Deleting the IAM role and instance profile"
aws iam remove-role-from-instance-profile \
  --instance-profile-name "${NAME}-backup-profile" --role-name "${NAME}-backup-role" >/dev/null 2>&1 || true
aws iam delete-instance-profile --instance-profile-name "${NAME}-backup-profile" >/dev/null 2>&1 || true
aws iam delete-role-policy --role-name "${NAME}-backup-role" --policy-name "backup-write" >/dev/null 2>&1 || true
aws iam delete-role --role-name "${NAME}-backup-role" >/dev/null 2>&1 || true
echo "    deleted"

cat <<DONE

  Done. The backup bucket $BUCKET was kept.
  To remove it too (this erases every backup):

      aws s3 rb s3://$BUCKET --force

DONE
