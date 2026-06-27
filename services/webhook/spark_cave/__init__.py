"""Spark Cave -- shared async owned-GPU LLM fallback over an SQS airlock.

Persona-generic: distinct services (e.g. a `code-review` persona and a
`meal-gen` persona) share the schema, payload packing, enqueue, and
result-handling, in separate per-service queues.
Import the submodules (`schema`, `packing`, `enqueue`, `results`) directly so a
consumer that only needs the schema does not pull in the boto3-touching paths.
"""
