# Budget contract

Each worker node requires id, backend="worker", workspace, objective, spec, inputs, outputs, checks and acceptance_argv. Choose budget_profile quick/standard/extended; custom budget and extended need budget_reason. Fields: implementation_sec, verification_sec, max_commands, max_patch_errors, idle_sec, max_output_files. Profiles are starting estimates, not optimal promises. Run bin/budget.py --help for local estimate options.

Default standard implementation/verification budgets are 120/90s; quick 60/30, extended 240/180. A team-wide timeout remains authoritative. No automatic extensions. Long local tests receive verification time, not more model time. Scope changes hand back.

Default two independent worker packets per plan; three needs max_worker_packets=3 and wave_reason. Paid descendants after worker require main-agent diff acceptance and a new plan; local validators may continue automatically. Successful local checks leave main_agent_acceptance pending. Runtime events and file hashes are evidence, not permission or a filesystem sandbox.

The example plan uses a local-only node and may be executed without a model. Replace its node deliberately when designing a real worker contract.
