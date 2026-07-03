# Vendored: spark_cave

Canonical source: **githumps/infra-public** `python/spark_cave/spark_cave/`
Pinned at: `b8d8b3f5acf5bc5e233b8f2a76029633e5070089`

Do NOT edit these files in grug. Fix upstream in infra-public, then re-vendor:

    SHA=<infra-public-sha>
    cp <infra-public>/python/spark_cave/spark_cave/*.py services/webhook/spark_cave/

The shared SQS-airlock library (schema / packing / enqueue / results), public +
zero-runtime-dep, so grug (public) and somatic-scripts (private) share one wire
contract. See githumps/infra-public for the canonical package + its tests.
