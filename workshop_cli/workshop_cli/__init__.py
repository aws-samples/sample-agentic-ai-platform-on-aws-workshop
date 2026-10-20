"""The `workshop` command: `workshop apply <module>` builds one module of the learning path from design-record.yaml.

Every step reuses the platform SDK: the same names, the same idempotent creators, and one state file per project
(`.workshop/state/<project>.json`, git-ignored) that later modules and `workshop cleanup` read.
"""

__version__ = "0.1.0"
