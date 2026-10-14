import streamlit as st
import os, re, uuid, time, tempfile
from agent import new_conversation, handle_turn, transcribe_with_model, transcribe_local
from report_email import get_pending_email, confirm_and_send, cancel_pending_email, is_external

st.set_page_config(page_title="LIMS Copilot", layout="wide")


# ---------------------------------------------------------------- browser behaviour (one script, installed once)
#  * Enter in the message box sends (Shift+Enter = new line)
#  * opening the recorder starts recording by itself
#  * Enter stops the recording; Enter again sends the transcript
#  * the page scrolls to the newest message
_UI_JS = r"""
(() => {
  let D = document;
  if (!D.querySelector('[data-testid="stApp"]')) {          // running inside an iframe: use the parent page
    try { D = window.parent.document; } catch (e) { return; }
  }
  const W = D.defaultView;
  if (W.__limsUI_v2) return;                                // install only once
  W.__limsUI_v2 = true;

  const $ = (s) => D.querySelector(s);
  const widget = () => $('[data-testid="stAudioInput"]');
  const audioBtn = (re) => {
    const w = widget();
    if (!w) return null;
    return Array.from(w.querySelectorAll('button')).find(b =>
      re.test((b.getAttribute('aria-label') || '') + ' ' + (b.title || '') + ' ' + (b.textContent || ''))) || null;
  };
  const block = (e) => { e.preventDefault(); e.stopPropagation(); };

  // ---- send
  const send = () => {
    const ta = $('.st-key-draft textarea');
    if (ta && D.activeElement === ta) ta.blur();            // commits the text to Streamlit
    setTimeout(() => {
      const b = $('.st-key-send_btn button');
      if (b) b.click();
      setTimeout(() => { const t = $('.st-key-draft textarea'); if (t) t.focus(); }, 500);
    }, 80);
  };

  // ---- Enter key
  D.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.shiftKey || e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return;
    const t = e.target;
    const inDraft = t.closest && t.closest('.st-key-draft');
    const onMic = t.closest && t.closest('.st-key-mic_btn');
    if (!inDraft && !onMic && t !== D.body) return;         // never hijack other buttons

    const stop = audioBtn(/stop/i);
    if (stop) { block(e); stop.click(); return; }           // recording -> stop

    const ta = $('.st-key-draft textarea');
    if (ta && ta.value.trim()) { block(e); send(); return; } // text -> send

    if (widget()) {                                          // recorder open but idle -> start
      const rec = audioBtn(/record|start/i);
      if (rec) { block(e); rec.click(); return; }
    }
    if (inDraft) block(e);                                   // empty box: no stray new lines
  }, true);

  // ---- auto-start recording when the recorder appears
  let started = null;
  const autostart = () => {
    const w = widget();
    if (!w) { started = null; return; }
    const host = w.closest('[class*="st-key-rec_"]');
    const m = host && String(host.className).match(/st-key-rec_\d+/);
    const id = m ? m[0] : 'x';
    if (started === id) return;
    if (audioBtn(/stop/i)) { started = id; return; }
    const b = audioBtn(/record|start/i);
    if (b) { started = id; setTimeout(() => b.click(), 200); }
  };

  // ---- scroll to newest message
  const scroller = () => $('[data-testid="stMain"]') || $('section.main') || $('[data-testid="stAppViewContainer"]');
  const count = () => D.querySelectorAll('[data-testid="stChatMessage"]').length;
  let seen = count(), lastH = 0, queued = false;
  const tick = () => {
    queued = false;
    const m = scroller(), n = count();
    if (m) {
      const h = m.scrollHeight;
      const nearBottom = (lastH - (m.scrollTop + m.clientHeight)) < 250;
      if (n > seen || (h > lastH && nearBottom)) m.scrollTo({ top: h, behavior: 'smooth' });
      lastH = h;
    }
    seen = n;
    autostart();
  };
  new MutationObserver(() => {
    if (!queued) { queued = true; requestAnimationFrame(tick); }
  }).observe(D.body, { childList: true, subtree: true });
  tick();
})();
"""


def _transcribe(audio_file, retries=3):
    audio_bytes = audio_file.getvalue()
    mime = audio_file.type or "audio/wav"

    for attempt in range(retries):
        try:
            return transcribe_with_model(audio_bytes, mime, translate=True)
        except RuntimeError:
            raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))

    # Model failed on every try: fall back to local Whisper
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name
    try:
        return transcribe_local(tmp_path, translate=True)
    finally:
        os.remove(tmp_path)


st.markdown("""
<style>
/* hidden holder for the script below */
.st-key-js_host { display: none; }

/* round icon-only buttons */
.st-key-mic_btn button, .st-key-send_btn button {
    width: 3rem !important;
    height: 3rem !important;
    min-height: 3rem !important;
    padding: 0 !important;
    border-radius: 50% !important;
}
.st-key-mic_btn, .st-key-send_btn { display: flex; justify-content: center; }

/* chat row: text box takes the free space, the two buttons stay fixed-size */
.st-key-chat_row [data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap !important;
    gap: 0.6rem;
    align-items: center;
}
.st-key-chat_row [data-testid="stHorizontalBlock"] > div:nth-child(1) {
    flex: 1 1 auto !important;
    min-width: 0 !important;
}
.st-key-chat_row [data-testid="stHorizontalBlock"] > div:nth-child(n+2) {
    flex: 0 0 3.2rem !important;
    width: 3.2rem !important;
    min-width: 3.2rem !important;
}
</style>
""", unsafe_allow_html=True)

with st.container(key="js_host"):
    try:
        st.html(f"<script>{_UI_JS}</script>", unsafe_allow_javascript=True)
    except TypeError:                                   # older Streamlit without unsafe_allow_javascript
        import streamlit.components.v1 as components
        components.html(f"<script>{_UI_JS}</script>", height=0)

PREVIEW_WIDTH = 600
PREVIEW_HEIGHT = 400

st.session_state.setdefault("draft", "")
st.session_state.setdefault("show_recorder", False)
st.session_state.setdefault("rec_n", 0)           # a new recorder after each use, so it starts empty


def _toggle_recorder():
    st.session_state.show_recorder = not st.session_state.show_recorder


def _submit():
    text = st.session_state.draft.strip()
    if text:
        st.session_state.pending_input = text
        st.session_state.draft = ""


def _basename(path):
    return path.replace("\\", "/").split("/")[-1]


def _extract_plot_dimensions(html_content, default=(1100, 700)):
    w_match = re.search(r'"width"\s*:\s*(\d+)', html_content)
    h_match = re.search(r'"height"\s*:\s*(\d+)', html_content)

    width = int(w_match.group(1)) if w_match else 0
    height = int(h_match.group(1)) if h_match else 0

    if width <= 0:
        width = default[0]
    if height <= 0:
        height = default[1]

    return width, height


def _render_chart_png(path, key_prefix=""):
    if not os.path.exists(path):
        st.warning(f"This chart is no longer available: {_basename(path)}")
        return
    with open(path, "rb") as f:
        image_bytes = f.read()
    st.image(image_bytes, width=PREVIEW_WIDTH)
    st.download_button(
        "Download chart (PNG)", data=image_bytes,
        file_name=_basename(path), key=f"{key_prefix}_dl_{path}",
    )


def _render_chart_html(path, key_prefix=""):
    if not os.path.exists(path):
        st.warning(f"This chart is no longer available: {_basename(path)}")
        return
    with open(path, "r", encoding="utf-8") as f:
        html_content = f.read()
    real_width, real_height = _extract_plot_dimensions(html_content)
    scale = min(PREVIEW_WIDTH / real_width, PREVIEW_HEIGHT / real_height, 1.0) if real_width and real_height else 1.0
    wrapper_pct = (100 / scale) if scale else 100
    preview_html = f"""
    <div style="width:{PREVIEW_WIDTH}px; height:{PREVIEW_HEIGHT}px; overflow:hidden; position:relative;">
        <div style="transform: scale({scale}); transform-origin: top left;
                    width:{wrapper_pct}%; height:{wrapper_pct}%;">
            {html_content}
        </div>
    </div>
    """
    st.iframe(preview_html, width=PREVIEW_WIDTH, height=PREVIEW_HEIGHT)
    st.download_button(
        "Download chart (HTML)", data=html_content,
        file_name=_basename(path), key=f"{key_prefix}_dl_{path}",
    )


def _render_artifact(artifact, key_prefix):
    path = artifact["path"]
    kind = artifact["type"]

    if kind == "chart_png":
        _render_chart_png(path, key_prefix=key_prefix)
    elif kind == "chart_html":
        _render_chart_html(path, key_prefix=key_prefix)
    elif kind == "excel":
        if os.path.exists(path):
            with open(path, "rb") as f:
                st.download_button(
                    "Download Excel file",
                    data=f,
                    file_name=_basename(path),
                    key=f"{key_prefix}_dl_{path}",
                )
        else:
            st.warning(f"This exported file is no longer available: {_basename(path)}")


if "messages" not in st.session_state:
    st.session_state.messages = new_conversation()

if "display_turns" not in st.session_state:
    st.session_state.display_turns = []

if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())

st.markdown(
    """
    <div style="text-align: center;">
        <h1 style="margin-bottom: 0;">Hola, señor!</h1>
        <p style="color: gray; margin-top: 0;">
            Welcome to Qualitec group's AI portal, how should I assist you today.
        </p>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    if st.button("New conversation", use_container_width=True):
        st.session_state.messages = new_conversation()
        st.session_state.display_turns = []
        st.session_state.session_id = str(uuid.uuid4())
        st.rerun()


for turn_idx, turn in enumerate(st.session_state.display_turns):
    with st.chat_message(turn["role"]):
        st.markdown(turn["content"])
        for art_idx, artifact in enumerate(turn.get("artifacts", [])):
            _render_artifact(artifact, key_prefix=f"turn{turn_idx}_art{art_idx}")


user_input = st.session_state.pop("pending_input", None)

if user_input:
    st.session_state.display_turns.append({"role": "user", "content": user_input, "artifacts": []})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            reply, st.session_state.messages, artifacts = handle_turn(
                st.session_state.messages, user_input, session_id=st.session_state.session_id, user="local-test"
            )
        st.markdown(reply)
        for art_idx, artifact in enumerate(artifacts):
            _render_artifact(artifact, key_prefix=f"turnNEW_art{art_idx}")

    st.session_state.display_turns.append({"role": "assistant", "content": reply, "artifacts": artifacts})


pending = get_pending_email(st.session_state.session_id, user="local-test")
if pending:
    c = pending["contact"]
    who = f"{c['name']} ({c['org']}) " if c.get("name") else ""
    warn = "  ⚠ outside the organisation" if is_external(c["email"]) else ""
    if c.get("source") == "shown":
        warn += "⚠ this address was not typed by you, check it"
    names = ", ".join(_basename(f) for f in (pending.get("files") or [pending["file"]]))
    st.warning(f"Send **{names}** to {who}**{c['email']}**?{warn}")
    with st.expander("Subject and message"):
        st.write(f"**{pending['subject']}**")
        st.write(pending["body"])
    _, col_a, _gap, col_b = st.columns([6, 1.6, 0.6, 1.6])
    if col_a.button("Send", key="email_send", icon=":material/send:", type="primary", use_container_width=True):
        ok, msg = confirm_and_send(st.session_state.session_id, user="local-test")
        st.session_state.display_turns.append({"role": "assistant", "content": msg, "artifacts": []})
        st.rerun()
    if col_b.button("Cancel", key="email_cancel", icon=":material/close:", use_container_width=True):
        cancel_pending_email(st.session_state.session_id, user="local-test")
        st.rerun()


if "voice_result" in st.session_state:                 # must run before the text area is created
    heard = st.session_state.pop("voice_result")
    st.session_state.draft = f"{st.session_state.draft} {heard}".strip()

bottom = getattr(st, "bottom", None) or st._bottom     # the same area st.chat_input uses
with bottom:
    # ---- recorder bar, directly above the chat box ----
    if st.session_state.show_recorder:
        note = st.session_state.pop("voice_note", None)
        if note:
            st.warning(note)
        st.caption("Recording starts automatically. Press Enter to stop.")
        audio = st.audio_input("Record", label_visibility="collapsed",
                               key=f"rec_{st.session_state.rec_n}")
        if audio is not None:
            try:
                with st.spinner("Transcribing and translating..."):
                    heard = _transcribe(audio)
            except Exception as e:
                heard = ""
                st.session_state.voice_note = f"Transcription failed: {e}"
            if heard:
                st.session_state.voice_result = heard
                st.session_state.show_recorder = False          # close the bar
            elif "voice_note" not in st.session_state:
                st.session_state.voice_note = "No speech detected. Record again."
            st.session_state.rec_n += 1
            st.rerun()

    # ---- chat box row ----
    with st.container(key="chat_row"):
        col_text, col_mic, col_send = st.columns([10, 1, 1], vertical_alignment="center")

        with col_text:
            st.text_area("Message", key="draft", height=68, label_visibility="collapsed",
                         placeholder="Ask about lab data, procedures, or reports... (Enter to send)")

        with col_mic:
            opened = st.session_state.show_recorder
            st.button("", key="mic_btn",
                      icon=":material/close:" if opened else ":material/mic:",
                      on_click=_toggle_recorder,
                      help="Close recorder" if opened else "Speak your question")

        with col_send:
            st.button("", key="send_btn", type="primary", icon=":material/send:",
                      on_click=_submit, help="Send")