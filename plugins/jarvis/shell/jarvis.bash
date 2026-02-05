# Jarvis - Global AI assistant
# Concatenates system prompts from all installed Jarvis plugins (modular architecture)
# Source: https://github.com/rsprudencio/claude-plugins
unalias jarvis 2>/dev/null
function jarvis() {
    local cache_dir="$HOME/.claude/plugins/cache/raph-claude-plugins"
    local system_prompt=""
    local settings_file=""
    local found_core=false

    # Load prompts from all Jarvis plugins (order matters: core first)
    for plugin in jarvis jarvis-todoist jarvis-strategic; do
        # Find versioned directory (e.g., jarvis/1.0.0/system-prompt.md)
        local prompt_file=$(find "$cache_dir/$plugin" -name "system-prompt.md" 2>/dev/null | head -1)
        if [[ -f "$prompt_file" ]]; then
            system_prompt+="$(cat "$prompt_file")"$'\n\n---\n\n'
            # Use core plugin's settings.json
            if [[ "$plugin" == "jarvis" ]]; then
                found_core=true
                settings_file="$(dirname "$prompt_file")/settings.json"
            fi
        fi
    done

    # Require core plugin
    if [[ "$found_core" == false ]]; then
        echo "Error: Jarvis core plugin not installed."
        echo "Install with: claude plugin install jarvis@raph-claude-plugins"
        return 1
    fi

    # Launch Claude with concatenated prompts
    local cmd="__JARVIS_CLAUDE_STATUSLINE__=1 claude --append-system-prompt \"\$system_prompt\""
    if [[ -f "$settings_file" ]]; then
        __JARVIS_CLAUDE_STATUSLINE__=1 claude \
            --append-system-prompt "$system_prompt" \
            --settings "$settings_file" \
            "$@"
    else
        __JARVIS_CLAUDE_STATUSLINE__=1 claude \
            --append-system-prompt "$system_prompt" \
            "$@"
    fi
}
