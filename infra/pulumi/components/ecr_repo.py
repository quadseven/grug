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
) -> aws.ecr.Repository:
    """Create a private ECR repo with lifecycle pruning."""
    repo = aws.ecr.Repository(
        name,
        name=name,
        image_scanning_configuration=aws.ecr.RepositoryImageScanningConfigurationArgs(
            scan_on_push=True,
        ),
        image_tag_mutability="MUTABLE",
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
