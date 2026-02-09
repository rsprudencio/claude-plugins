# Source: https://github.com/rsprudencio/claude-plugins
unalias jarvis 2>/dev/null
jarvis() {
    local system_prompt=""
    local settings_file=""
    local found_core=false

    # Get installed plugin paths from Claude's plugin system (canonical source of truth)
    local plugin_paths
    plugin_paths=$(claude plugin list --json 2>/dev/null | python3 -c "
import sys, json
for p in json.load(sys.stdin):
    pid = p.get('id', '')
    if pid.startswith('jarvis'):
        print(f\"{pid.split('@')[0]}|{p['installPath']}\")
" 2>/dev/null)

    local name plugin_path
    while IFS='|' read -r name plugin_path; do
        [ -z "$name" ] && continue
        if [ -f "$plugin_path/system-prompt.md" ]; then
            system_prompt+="$(command cat "$plugin_path/system-prompt.md")"$'\n\n---\n\n'
            if [ "$name" = "jarvis" ]; then
                found_core=true
                settings_file="$plugin_path/settings.json"
            fi
        fi
    done <<< "$plugin_paths"

    # Require core plugin
    if [ "$found_core" = false ]; then
        echo "Error: Jarvis core plugin not installed."
        echo "Install with: claude plugin install jarvis@raph-claude-plugins"
        return 1
    fi

    # Launch Claude with concatenated prompts and settings
    local -a extra_args=()
    if [ -f "$settings_file" ]; then
        extra_args+=(--settings "$settings_file")
    fi

    __JARVIS_CLAUDE_STATUSLINE__=1 command claude \
        --append-system-prompt "$system_prompt" \
        "${extra_args[@]}" \
        "$@"
}
