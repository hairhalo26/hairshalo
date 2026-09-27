"""Move existing product photographs and videos from local disk to S3.

Why this exists: switching PRODUCT_MEDIA_STORAGE to s3 only changes where
NEW uploads go. Every product_media row written before the switch still
points at /media/<file> on this server. This copies each of those files into
the product bucket and repoints the row at it.

What it does, row by row (product_media with a storage_key only; rows that
point at an external URL were never ours to move):

* key already in S3 (starts with products/)   -> left alone
* local file exists                           -> uploaded to products/<same
                                                 name>, then url and
                                                 storage_key updated
* local file missing                          -> left alone and listed

The local file is NOT deleted: it stays as a copy until someone has checked
the storefront and removes it by hand. Each row is committed on its own
after its upload succeeds, so an interrupted run leaves every row pointing at
a file that exists, and running it again carries on where it stopped.

It is a dry run unless --apply is given. It needs PRODUCT_MEDIA_STORAGE=s3
and PRODUCT_MEDIA_BUCKET set, and runs inside the API container:

    docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm \\
      api python -m app.migrate_product_media            # dry run
    # the same with --apply to copy and repoint
"""
import argparse
import os
import sys

from app import models
from app.config import settings
from app.database import SessionLocal
from app.storage import S3_PREFIX, S3Storage, get_product_storage, get_storage


def run(apply: bool, product_ids=None) -> int:
    """product_ids limits the run to those products (None = every product)."""
    target = get_product_storage()
    if not isinstance(target, S3Storage):
        print("PRODUCT_MEDIA_STORAGE is not s3 (or PRODUCT_MEDIA_BUCKET is empty); "
              "nothing to migrate to.")
        return 2
    disk = get_storage()
    db = SessionLocal()
    moved = already = missing = 0
    try:
        q = (db.query(models.ProductMedia)
               .filter(models.ProductMedia.storage_key.isnot(None)))
        if product_ids is not None:
            q = q.filter(models.ProductMedia.product_id.in_(list(product_ids)))
        rows = q.order_by(models.ProductMedia.created_at).all()
        for m in rows:
            key = m.storage_key
            if key.startswith(S3_PREFIX):
                already += 1
                continue
            path = disk._path(key)
            if not os.path.isfile(path):
                missing += 1
                print(f"  MISSING  {m.id}  {path}")
                continue
            new_key = S3_PREFIX + os.path.basename(key)
            new_url = target.url_for(new_key)
            print(f"  {'COPY' if apply else 'would copy'}  {key} -> s3://{target.bucket}/{new_key}")
            if apply:
                with open(path, "rb") as fh:
                    target.put(fh, new_key)
                m.storage_key = new_key
                m.url = new_url
                db.commit()
            moved += 1
    finally:
        db.close()

    verb = "Copied" if apply else "Would copy"
    print(f"\n{verb} {moved} file(s) to s3://{target.bucket}/{S3_PREFIX}; "
          f"{already} already in S3; {missing} missing on disk.")
    if not apply and moved:
        print("Dry run: nothing was changed. Run again with --apply.")
    if apply and moved:
        print(f"The originals are still in {settings.MEDIA_ROOT}. Check the "
              "storefront, then remove them by hand if you want the space back.")
    return 1 if missing else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="copy the files and repoint the rows (default: dry run)")
    args = parser.parse_args(argv)
    sys.exit(run(args.apply))


if __name__ == "__main__":
    main()
