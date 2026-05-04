"""ECR repository factory with lifecycle policy.

Per PRD #21: untagged images expire after 14 days to avoid the image
graveyard cost (~$0.10/GB/mo) when CI rebuilds churn the repo.
"""

from __future__ import annotations

import json

import pulumi
import pulumi_aws as aws


def create(
    name: str,
    untagged_expire_days: int = 14,
    force_delete: bool = False,
) -> aws.ecr.Repository:
    """Create a private ECR repo with lifecycle pruning.

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
            {
                "rules": [
                    {
                        "rulePriority": 1,
                        "description": (
                            f"Expire untagged images older than "
                            f"{untagged_expire_days} days"
                        ),
                        "selection": {
                            "tagStatus": "untagged",
                            "countType": "sinceImagePushed",
                            "countUnit": "days",
                            "countNumber": untagged_expire_days,
                        },
                        "action": {"type": "expire"},
                    },
                ],
            },
        ),
    )
    return repo
