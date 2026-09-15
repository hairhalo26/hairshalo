#!/usr/bin/env bash
# Turn on Session Manager for the Hairshalo instance, so getting a shell no
# longer depends on port 22 being open to whatever address your ISP handed you
# today. Paste into AWS CloudShell (region: ap-south-1).
#
# Session Manager connects OUTBOUND from the instance to the SSM service and
# tunnels the session back down it. Nothing listens, nothing is allowed in, and
# there is no inbound rule to keep current - which is the whole point. A
# residential IP changes without warning, and the symptom is a bare
# "Connection timed out" that looks identical to a dead server.
#
# Three things have to be true, and this script handles the first:
#
#   1. The instance's IAM role allows the SSM APIs.
#   2. The SSM agent is running. Ubuntu 24.04 AMIs ship it as a snap.
#   3. The instance can reach the SSM endpoints outbound on 443. It has a
#      public IP behind an internet gateway and ufw allows all outgoing, so
#      this is already true. (A private instance would need VPC endpoints.)
#
# It deliberately does NOT close port 22. Removing your only way in before the
# replacement is proven is how a maintenance task becomes an incident. The
# commands to close it are printed at the end, to run once a session has worked.
set -euo pipefail

NAME="hairshalo"
REGION="ap-south-1"
ROLE="${NAME}-backup-role"
PROFILE="${NAME}-backup-profile"
SSM_POLICY="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"

export AWS_DEFAULT_REGION="$REGION"
export AWS_PAGER=""

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    \033[33m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. Find the instance
# ---------------------------------------------------------------------------
say "Finding the ${NAME}-prod instance"

INSTANCE_ID="$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=${NAME}-prod" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)"

if [ "$INSTANCE_ID" = "None" ] || [ -z "$INSTANCE_ID" ]; then
  echo "No running instance tagged ${NAME}-prod in $REGION." >&2
  echo "Check the region selector, or that the instance is not stopped." >&2
  exit 1
fi
info "$INSTANCE_ID"

# ---------------------------------------------------------------------------
# 2. Make sure it HAS an instance profile, and reuse the existing one
# ---------------------------------------------------------------------------
# An instance can carry exactly one instance profile, so the SSM permission has
# to join the role already attached rather than arrive on a second role of its
# own. That role is the backup role from cloudshell-provision.sh.
say "Instance profile"

ATTACHED="$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].IamInstanceProfile.Arn' --output text)"

if [ "$ATTACHED" = "None" ] || [ -z "$ATTACHED" ]; then
  warn "no instance profile attached - attaching $PROFILE"
  aws ec2 associate-iam-instance-profile \
    --instance-id "$INSTANCE_ID" \
    --iam-instance-profile "Name=$PROFILE" >/dev/null
  info "attached"
else
  info "already attached: ${ATTACHED##*/}"
fi

# ---------------------------------------------------------------------------
# 3. Add the SSM permission to that role
# ---------------------------------------------------------------------------
# AmazonSSMManagedInstanceCore is the AWS-managed policy for exactly this. It
# grants the SSM APIs an instance needs in order to register and hold a
# session, and nothing else - it is not administrative access to the account.
say "Granting SSM access to $ROLE"

if aws iam list-attached-role-policies --role-name "$ROLE" \
     --query 'AttachedPolicies[].PolicyArn' --output text | grep -q "$SSM_POLICY"; then
  info "already attached"
else
  aws iam attach-role-policy --role-name "$ROLE" --policy-arn "$SSM_POLICY"
  info "attached AmazonSSMManagedInstanceCore"
fi

# ---------------------------------------------------------------------------
# 4. Wait for the instance to register with SSM
# ---------------------------------------------------------------------------
# The agent caches its credentials, so a freshly granted policy is not picked
# up instantly. Restarting the agent is the fast path, but that needs a shell -
# and not having one is often why you are here. So: wait, then say how to hurry
# it along if the wait runs out.
say "Waiting for $INSTANCE_ID to register with SSM (up to 5 minutes)"

registered=""
for _ in $(seq 1 30); do
  status="$(aws ssm describe-instance-information \
    --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
    --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || echo "None")"
  if [ "$status" = "Online" ]; then
    registered="yes"
    break
  fi
  printf '.'
  sleep 10
done
printf '\n'

if [ -n "$registered" ]; then
  info "Online"
else
  warn "not registered yet."
  warn "Give it a few more minutes and re-run this script - the agent refreshes"
  warn "its credentials on its own schedule. If you still have SSH, this forces it:"
  warn "    sudo snap restart amazon-ssm-agent"
fi

# ---------------------------------------------------------------------------
# 5. What to do next
# ---------------------------------------------------------------------------
say "Connect - no SSH key, no inbound rule, no allowlist to keep current"

cat <<CONNECT

  In the console (nothing to install):

      EC2 -> Instances -> select $INSTANCE_ID -> Connect
           -> "Session Manager" tab -> Connect

  Or from CloudShell:

      aws ssm start-session --target $INSTANCE_ID

CONNECT

# Quoted heredoc from here: every $ and backtick below is meant to reach the
# screen literally, as commands for you to run rather than values to expand now.
cat <<'NEXT'
  ---------------------------------------------------------------------------
  YOU LAND AS ssm-user, NOT ubuntu - this matters
  ---------------------------------------------------------------------------

  Session Manager drops you in as ssm-user, which is not in the docker group.
  Every deploy command fails with a permission error until you switch:

      sudo su - ubuntu

  Then the usual work, exactly as over SSH:

      cd /srv/hairshalo/vera-full-project
      git pull
      ./scripts/reload-caddy.sh

  ---------------------------------------------------------------------------
  ONLY ONCE A SESSION HAS ACTUALLY WORKED - close port 22
  ---------------------------------------------------------------------------

  Open a session, run 'sudo su - ubuntu', confirm you have a working shell.
  THEN remove the SSH rules, and not before. In CloudShell:

      SG=$(aws ec2 describe-security-groups \
        --filters "Name=group-name,Values=hairshalo-sg" \
        --query 'SecurityGroups[0].GroupId' --output text)

      aws ec2 describe-security-group-rules \
        --filters "Name=group-id,Values=$SG" \
        --query 'SecurityGroupRules[?FromPort==`22`].[SecurityGroupRuleId,CidrIpv4]' \
        --output text

  That prints every SSH rule with its source. Remove them one at a time:

      aws ec2 revoke-security-group-ingress \
        --group-id $SG --security-group-rule-ids sgr-xxxxxxxx

  Use revoke-security-group-ingress rather than editing rules in the console.
  Changing a rule's Type in the console keeps its old Source, which is how an
  HTTP rule open to 0.0.0.0/0 quietly becomes an SSH rule open to 0.0.0.0/0.

  Leave ports 80 and 443 alone - 80 is the HTTPS redirect and Let's Encrypt's
  HTTP challenge, 443 is the site itself.

  Session Manager keeps working with 22 closed. That is the point: nothing to
  update the next time your address changes, and no open SSH port for the
  internet to knock on.

NEXT
