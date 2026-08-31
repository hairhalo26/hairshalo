#!/usr/bin/env bash
# Set up Amazon SES for Hairshalo and print the exact DNS records to add at
# GoDaddy. Paste into AWS CloudShell (region: ap-south-1).
#
# This creates the domain identity and turns on Easy DKIM. It CANNOT request
# production access - that is a support case and has to be raised in the
# console. Until it is granted, SES only delivers to addresses you have
# verified, so a real customer's order confirmation is refused outright.
set -euo pipefail

DOMAIN="hairshalo.com"
DMARC_RUA="studio@hairshalo.com"     # where aggregate DMARC reports go
REGION="ap-south-1"

export AWS_DEFAULT_REGION="$REGION"
export AWS_PAGER=""

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# SES identities are regional. A domain verified in one region cannot send
# from another region's SMTP endpoint, and the failure looks like a
# credentials problem rather than a region problem.
say "Creating the SES identity for $DOMAIN in $REGION"

if aws sesv2 get-email-identity --email-identity "$DOMAIN" >/dev/null 2>&1; then
  echo "    identity already exists - reading its DKIM tokens"
else
  aws sesv2 create-email-identity \
    --email-identity "$DOMAIN" \
    --dkim-signing-attributes NextSigningKeyLength=RSA_2048_BIT >/dev/null
  echo "    created"
fi

TOKENS="$(aws sesv2 get-email-identity --email-identity "$DOMAIN" \
  --query 'DkimAttributes.Tokens' --output text)"

STATUS="$(aws sesv2 get-email-identity --email-identity "$DOMAIN" \
  --query 'VerifiedForSendingStatus' --output text)"

say "Add these records at GoDaddy (DNS -> Manage Zones)"

cat <<HEADER

  GoDaddy appends your domain automatically. Enter the Name column EXACTLY as
  shown - adding ".$DOMAIN" yourself produces
  "xxxx._domainkey.$DOMAIN.$DOMAIN" and verification never completes.

  TYPE   NAME                                          VALUE
  ----   ----                                          -----
HEADER

for t in $TOKENS; do
  printf '  CNAME  %-44s %s\n' "${t}._domainkey" "${t}.dkim.amazonses.com"
done

cat <<RECORDS
  TXT    @                                             v=spf1 include:amazonses.com ~all
  TXT    _dmarc                                        v=DMARC1; p=none; rua=mailto:${DMARC_RUA}

  If an SPF record already exists, MERGE into that one record rather than
  adding a second. Two SPF records is a permanent error and fails both:
      v=spf1 include:amazonses.com include:_spf.google.com ~all

  DMARC p=none monitors without affecting delivery. Tighten to p=quarantine
  once reports look clean for a few weeks.

RECORDS

say "Current sending status: $STATUS"

cat <<'NEXT'

  STILL TO DO IN THE CONSOLE - neither can be scripted:

  1. Request production access
       SES -> Account dashboard -> Request production access
     Describe it honestly: transactional order confirmations and shipping
     updates for your own store. Approval is usually under 24 hours.

  2. Create SMTP credentials
       SES -> SMTP settings -> Create SMTP credentials
     These are NOT your AWS access keys. An IAM secret key pasted into
     SMTP_PASSWORD fails to authenticate - SES derives a different
     credential. The password is shown once; download the CSV.

     Then in .env.prod on the instance:
       SMTP_HOST=email-smtp.ap-south-1.amazonaws.com
       SMTP_PORT=587
       SMTP_USERNAME=<from the CSV>
       SMTP_PASSWORD=<from the CSV>

  While you wait for production access, verify your own address so you can
  test end to end:
       aws sesv2 create-email-identity --email-identity you@example.com

NEXT
