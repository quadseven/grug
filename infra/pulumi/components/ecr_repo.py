"""ECR repository factory with lifecycle policy.

Per PRD #21: untagged images expire after 14 days to avoid the image
graveyard cost (~$0.10/GB/mo) when CI rebuilds churn the repo.
"""

from __future__ import annotations

import json

import pulumi
import pulumi_aws as aws


def lifecycle_rules(
    untagged_expire_days: int, keep_last_images: int | None
) -> list[dict]:
    """PURE: build the ECR lifecycle rule list (no Pulumi/AWS) so the policy
    shape is unit-testable. Rule 1 prunes the untagged graveyard; rule 2 (only
    when keep_last_images is set) caps total images via a `tagStatus: any`
    count rule — which ECR requires to carry the highest rulePriority."""
    rules: list[dict] = [
        {
            "rulePriority": 1,
            "description": (
                f"Expire untagged images older than {untagged_expire_days} days"
            ),
            "selection": {
                "tagStatus": "untagged",
                "countType": "sinceImagePushed",
                "countUnit": "days",
                "countNumber": untagged_expire_days,
            },
            "action": {"type": "expire"},
        },
    ]
    if keep_last_images is not None:
        rules.append(
            {
                "rulePriority": 2,
                "description": f"Keep only the last {keep_last_images} images (any tag)",
                "selection": {
                    "tagStatus": "any",
                    "countType": "imageCountMoreThan",
                    "countNumber": keep_last_images,
                },
                "action": {"type": "expire"},
            }
        )
    return rules


def create(
    name: str,
    untagged_expire_days: int = 14,
    keep_last_images: int | None = None,
    force_delete: bool = False,
) -> aws.ecr.Repository:
    """Create a private ECR repo with lifecycle pruning.

    `untagged_expire_days` prunes the untagged graveyard. But CI tags every
    build image with its commit SHA, so untagged-only pruning lets TAGGED
    images accumulate forever (grug-api/webhook hit 200+ images, ~all of the
    repo's billed unique-layer storage). `keep_last_images`, when set, adds a
    `tagStatus: any` + `imageCountMoreThan` rule that keeps only the N most
    recent images (the live Lambda image is always the newest, so N≈20 leaves
    generous rollback headroom). None preserves the old untagged-only behavior.

    `force_delete` is opt-in per stack. Slice 10 #31 `make rebuild`
    needs it for the dev stack so `pulumi destroy` can wipe non-empty
    repos; prod must default to False so `pulumi destroy --stack prod`
    cannot silently delete production images. Greptile P2 PR #59.
    """
    repo = aws.ecr.Repository(
        name,
        name=name,
        image_scanning_configuration=aws.ecr.RepositoryImageScanningConfigurationArgs(
            scan_on_push=True,
        ),
        image_tag_mutability="MUTABLE",
        force_delete=force_delete,
        tags={"app": "grug", "managed-by": "pulumi"},
    )

    aws.ecr.LifecyclePolicy(
        f"{name}-lifecycle",
        repository=repo.name,
        policy=json.dumps(
            {"rules": lifecycle_rules(untagged_expire_days, keep_last_images)}
        ),
    )
    return repo
