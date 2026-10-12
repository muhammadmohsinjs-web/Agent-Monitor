// Copy to ~/.config/opencode/plugins/agent-monitor.js : starts the monitor whenever OpenCode starts.
// Set AGENT_MONITOR_DIR to the folder you cloned Agent-Monitor into (default: ~/Agent-Monitor).
export const AgentMonitor = async ({ $ }) => {
  const dir = process.env.AGENT_MONITOR_DIR || `${process.env.HOME}/Agent-Monitor`
  try { await $`${dir}/autostart/ensure.sh`.quiet() } catch (_) {}
  return {}
}
