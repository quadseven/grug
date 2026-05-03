"""Account-agnostic Pulumi component factories.

Per PRD #21: components are pure factories that take all dependencies as
args (provider, names, refs). When AWS Organizations growth happens
(billing/cicd/workload account split), only the composition root changes
which provider it hands each factory — components stay.
"""
