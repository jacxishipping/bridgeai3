# Example Action flows

## 1. Fleet pulse
1. `healthReady`
2. `listDevices`
3. Summarize online hostnames + OS

## 2. See the screen
1. `listDevices` → pick Windows `device_id`
2. `getDesktopFrame` with `scale=0.5`, `quality=70`
3. Describe UI; do not invent pixels not present in the response

## 3. Click after inspect
1. `getUiElements` (`max_elements=40`)
2. Confirm the target with the user if ambiguous
3. `sendDesktopInput` `{ "action": "click", "x": 0.42, "y": 0.61, "normalized": true }`

## 4. Safe system info task
```json
POST /api/v1/tasks
{
  "device_id": "<id>",
  "tool": "get_system_info",
  "args": {},
  "human_initiated": true
}
```
Then `getTask` until `succeeded` / `failed` / still `needs_approval`.

## 5. Shell (confirm first)
```json
{
  "device_id": "<id>",
  "tool": "run_shell",
  "args": { "command": "Get-ComputerInfo | Select-Object CsName, WindowsVersion" },
  "human_initiated": true
}
```
If `needs_approval`, either guide dashboard approval or (admin token only) `decideApproval`.

## 6. Live session
1. `createSession` `{ "device_id": "<windows-id>", "mode": "view_only" }`
2. Tell user the session id / that the dashboard client can attach
3. `terminateSession` when finished
