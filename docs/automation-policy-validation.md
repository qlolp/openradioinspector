# Pure validation before external authority staging

`AutomationService::validated_policy_patch(kind, payload, session)` exposes the
existing closed policy validator and its existing 16 KiB payload bound without
admitting, allocating, enabling, or publishing a policy. It returns the same
normalized patch that configuration uses. Configuration still validates again
against its actual Session lookup before admission.

Core uses this pure helper before staging original Guard authority. A malformed
update therefore keeps the original validation message and cannot displace an
active policy or leak a supplied manual token into an event. The additive API
does not alter any serialized policy or status schema.
