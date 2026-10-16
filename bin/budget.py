"""Local task budgets. Presets are starting estimates, never learned optimal limits."""
PROFILES = {
    'quick': dict(implementation_sec=60, verification_sec=30, max_commands=8,
                  max_patch_errors=1, idle_sec=90, max_output_files=2),
    'standard': dict(implementation_sec=120, verification_sec=90, max_commands=16,
                     max_patch_errors=1, idle_sec=120, max_output_files=2),
    'extended': dict(implementation_sec=240, verification_sec=180, max_commands=24,
                     max_patch_errors=2, idle_sec=180, max_output_files=4),
}
BOUNDS = dict(implementation_sec=(1,600), verification_sec=(1,600), max_commands=(1,100),
              max_patch_errors=(1,5), idle_sec=(0,600), max_output_files=(1,8))


def resolve(node):
    profile = node.get('budget_profile', 'standard')
    if not isinstance(profile,str) or profile not in PROFILES:
        raise ValueError('unknown budget_profile')
    changes = node.get('budget', {})
    if not isinstance(changes, dict) or set(changes)-set(BOUNDS):
        raise ValueError('unknown budget fields')
    reason=node.get('budget_reason','')
    if not isinstance(reason,str):raise ValueError('budget_reason must be text')
    if (profile=='extended' or changes) and not reason.strip():
        raise ValueError('extended/custom budgets need budget_reason based on task or measurements')
    result = dict(PROFILES[profile])
    result.update(changes)
    # Legacy explicit node timeout selects implementation time; it is no longer the verifier budget.
    if 'timeout_sec' in node:
        result['implementation_sec'] = node['timeout_sec']
        if node['timeout_sec']>PROFILES[profile]['implementation_sec'] and not reason.strip():
            raise ValueError('larger implementation timeout needs budget_reason')
    for key,(low,high) in BOUNDS.items():
        if type(result[key]) is not int or not low<=result[key]<=high:
            raise ValueError(f'{key} must be {low}..{high}')
    return dict(result, profile=profile, rationale=reason.strip() or 'profile starting estimate')


if __name__=='__main__':
    import argparse
    import json
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', choices=PROFILES, default='standard')
    p.add_argument('--reason', default='')
    for name in ('implementation-sec','max-commands','max-patch-errors','idle-sec'):
        p.add_argument('--'+name,type=int)
    a=p.parse_args()
    node=dict(budget_profile=a.profile,budget_reason=a.reason,budget={})
    for name in ('implementation_sec','max_commands','max_patch_errors','idle_sec'):
        value=getattr(a,name)
        if value is not None:node['budget'][name]=value
    print(json.dumps(resolve(node)))
