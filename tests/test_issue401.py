"""
Regression tests for tool-card persistence on session reload.

The older loadSession() path rewrote message history on the client:
- dropped role='tool' rows
- dropped empty assistant rows even when they carried tool_calls
- then ignored session.tool_calls on reload

That broke both durable logging and page refresh for valid tool runs.
"""
import json
import pathlib
import subprocess
import textwrap

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_loadsession_preserves_tool_rows():
    """Reload must keep tool rows in S.messages so snippets can be reconstructed."""
    assert "if (m.role === 'tool') continue;" not in SESSIONS_JS, (
        "loadSession() must not drop role='tool' messages; renderMessages() hides them "
        "visually, but it still needs them for snippet reconstruction"
    )


def test_loadsession_uses_session_toolcalls_only_as_fallback():
    """Session summaries are the fallback, not the primary reload source."""
    assert ("if(!hasMessageToolMetadata&&data.session.tool_calls&&data.session.tool_calls.length)" in SESSIONS_JS or
            "if (!hasMessageToolMetadata && data.session.tool_calls && data.session.tool_calls.length)" in SESSIONS_JS)
    assert ("S.toolCalls=(data.session.tool_calls||[]).map(tc=>({...tc,done:true}));" in SESSIONS_JS or
            "S.toolCalls = data.session.tool_calls.map(tc => ({...tc, done: true}));" in SESSIONS_JS)
    assert "S.toolCalls=[];" in SESSIONS_JS


def test_rendermessages_hides_empty_toolcall_assistants_from_transcript():
    """Empty tool-call assistant rows stay in raw history but do not draw blank turns."""
    assert "const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;" in UI_JS
    assert "function _assistantHasVisibleTranscriptPayload(m)" in UI_JS
    assert "if(_assistantHasVisibleTranscriptPayload(m)) visWithIdx.push({m,rawIdx});" in UI_JS
    assert "hasTc||hasTu||_messageHasReasoningPayload(m)" not in UI_JS.replace(" ", "")


def _run_js(script_body: str) -> dict:
    script = textwrap.dedent(f"""
        function loadSessionShape(messages, sessionToolCalls) {{
            const filtered = (messages || []).filter(m => m && m.role);
            const hasMessageToolMetadata = filtered.some(m => {{
                if (!m || m.role !== 'assistant') return false;
                const hasTc = Array.isArray(m.tool_calls) && m.tool_calls.length > 0;
                const hasTu = Array.isArray(m.content) && m.content.some(p => p && p.type === 'tool_use');
                return hasTc || hasTu;
            }});
            const toolCalls = (!hasMessageToolMetadata && sessionToolCalls && sessionToolCalls.length)
                ? sessionToolCalls.map(tc => ({{ ...tc, done: true }}))
                : [];
            return {{ filtered, hasMessageToolMetadata, toolCalls }};
        }}

        {script_body}
    """)
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def test_reload_keeps_empty_assistant_toolcall_anchor():
    """OpenAI-style assistant {content:'', tool_calls:[...]} must survive reload."""
    result = _run_js("""
        const messages = [
            { role: 'user', content: 'list files' },
            {
                role: 'assistant',
                content: '',
                tool_calls: [{ id: 'call-1', function: { name: 'terminal', arguments: '{}' } }]
            },
            { role: 'tool', tool_call_id: 'call-1', content: '{"output":"ok"}' },
            { role: 'assistant', content: 'Done.' }
        ];
        const loaded = loadSessionShape(messages, [{ name: 'terminal', assistant_msg_idx: 1 }]);
        process.stdout.write(JSON.stringify({
            filtered_len: loaded.filtered.length,
            has_metadata: loaded.hasMessageToolMetadata,
            fallback_len: loaded.toolCalls.length,
            assistant_tool_idx: loaded.filtered.findIndex(m => m.role === 'assistant' && m.tool_calls),
            tool_idx: loaded.filtered.findIndex(m => m.role === 'tool')
        }));
    """)
    assert result["filtered_len"] == 4
    assert result["has_metadata"] is True
    assert result["fallback_len"] == 0
    assert result["assistant_tool_idx"] == 1
    assert result["tool_idx"] == 2


def test_visible_transcript_excludes_empty_toolcall_anchor():
    """The UI should not show a blank assistant row for tool-call-only messages."""
    result = _run_js("""
        function msgContent(m){
            let c=m.content||'';
            if(Array.isArray(c)) c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('').trim();
            return String(c).trim();
        }
        function _messageHasReasoningPayload(m){
            if(!m||m.role!=='assistant') return false;
            if(m.reasoning) return true;
            if(Array.isArray(m.content)) return m.content.some(p=>p&&(p.type==='thinking'||p.type==='reasoning'));
            return /<think>[\\s\\S]*?<\\/think>/.test(String(m.content||''));
        }
        function _assistantHasVisibleTranscriptPayload(m){
            if(!m||m.role!=='assistant') return false;
            return !!(msgContent(m)||m.attachments?.length||_messageHasReasoningPayload(m));
        }
        function visible(messages){
            const out=[];
            let rawIdx=0;
            for(const m of messages){
                if(!m||!m.role||m.role==='tool'){rawIdx++;continue;}
                if(m.role==='assistant'){
                    if(_assistantHasVisibleTranscriptPayload(m)) out.push({role:m.role, rawIdx});
                }else if(msgContent(m)||m.attachments?.length){
                    out.push({role:m.role, rawIdx});
                }
                rawIdx++;
            }
            return out;
        }
        const messages = [
            { role: 'user', content: 'search sessions' },
            { role: 'assistant', content: '', tool_calls: [{ id: 'call-1' }] },
            { role: 'tool', tool_call_id: 'call-1', content: '{"output":"ok"}' },
            { role: 'assistant', content: [{ type: 'tool_use', id: 'toolu-1', name: 'session_search' }] },
            { role: 'assistant', content: [{ type: 'reasoning', text: 'Checking memory.' }] },
            { role: 'assistant', content: 'Found 17 sessions.' }
        ];
        process.stdout.write(JSON.stringify({ visible: visible(messages) }));
    """)
    assert result["visible"] == [
        {"role": "user", "rawIdx": 0},
        {"role": "assistant", "rawIdx": 4},
        {"role": "assistant", "rawIdx": 5},
    ]


def test_reload_uses_session_summary_when_messages_have_no_tool_metadata():
    """Older sessions should still render from session.tool_calls on reload."""
    result = _run_js("""
        const messages = [
            { role: 'user', content: 'build site' },
            { role: 'assistant', content: 'Starting.' },
            { role: 'tool', content: '{"bytes_written": 4955}' },
            { role: 'assistant', content: '' }
        ];
        const sessionToolCalls = [
            { name: 'write_file', assistant_msg_idx: 1, snippet: 'bytes_written', tid: '' }
        ];
        const loaded = loadSessionShape(messages, sessionToolCalls);
        process.stdout.write(JSON.stringify({
            has_metadata: loaded.hasMessageToolMetadata,
            fallback_len: loaded.toolCalls.length,
            done_flag: loaded.toolCalls[0] && loaded.toolCalls[0].done === true
        }));
    """)
    assert result["has_metadata"] is False
    assert result["fallback_len"] == 1
    assert result["done_flag"] is True
