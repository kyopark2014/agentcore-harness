import streamlit as st
import chat
import json
import logging
import sys
import skill
import mcp_config
import utils
import agentcore_client
from notification_queue import NotificationQueue

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("streamlit")

st.set_page_config(
    page_title="Harness",
    page_icon=None,
    layout="centered",
    initial_sidebar_state="auto",
    menu_items=None,
)

mode_descriptions = {
    "Agent": [
        "SKILL과 MCP를 활용한 Harness Agent를 이용합니다. 왼쪽 메뉴에서 필요한 Skill/MCP를 선택하세요."
    ],
}

uploaded_file = None

# ---- Entry: require user_id (used as actor_id) before the main UI ----
if "user_id" not in st.session_state:
    st.session_state.user_id = ""

if not st.session_state.user_id:
    st.title("🔮 사용자 아이디 입력")
    st.markdown(
        "시작하려면 아이디를 입력하세요. "
    )
    with st.form("login_form", clear_on_submit=False):
        login_id = st.text_input(
            "아이디",
            placeholder="예: user01",
            help="영문·숫자·._- 만 사용하세요. 그 외 문자는 _ 로 치환됩니다.",
        )
        submitted = st.form_submit_button("시작", type="primary")
    if submitted:
        chat.set_user_id(login_id)
        if not chat.user_id:
            st.error("아이디를 입력해주세요.")
        else:
            st.session_state.user_id = chat.user_id
            st.rerun()
    st.stop()

chat.set_user_id(st.session_state.user_id)

with st.sidebar:
    st.title("🔮 Menu")

    st.markdown(
        "Amazon의 AgentCore Harness를 이용해 Agent를 구현합니다. "
        "여기에서는 SKILL과 MCP를 이용해 agent의 기능을 확장합니다.\n"
        "상세한 코드는 [Github](https://github.com/kyopark2014/agentcore-harness)을 참조하세요."
    )

    st.subheader("👤 사용자")
    st.info(f"아이디: `{st.session_state.user_id}`")
    if st.button("아이디 변경", key="change_user_id"):
        st.session_state.user_id = ""
        st.session_state.messages = []
        st.session_state.greetings = False
        chat.set_user_id(None)
        chat.initiate()
        st.rerun()

    st.subheader("🐱 대화 형태")

    mode = st.radio(
        label="원하는 대화 형태를 선택하세요. ",
        options=["Agent"],
        index=0,
    )
    st.info(mode_descriptions[mode][0])

    # Skill / MCP selection
    st.subheader("⚙️ Skill Config")

    skill_selections = {}
    default_skill_selections, default_mcp_selections = utils.get_initial_tool_defaults()
    logger.info(f"default_skill_selections: {default_skill_selections}")

    with st.expander("Skill 옵션 선택", expanded=True):
        if skill.skill_managers.get("base") is None:
            skill.register_plugin_skills("base")
        available_skill_info = skill.available_skill_info("base")
        for s in available_skill_info:
            default_value = s["name"] in default_skill_selections
            skill_selections[s["name"]] = st.checkbox(
                s["name"],
                key=f"skill_{s['name']}",
                value=default_value,
                help=s["description"],
                disabled=False,
            )

    selected_skills = [name for name, is_selected in skill_selections.items() if is_selected]
    logger.info(f"selected_skills: {selected_skills}")

    st.subheader("⚙️ MCP Config")

    mcp_selections = {}
    with st.expander("MCP 옵션 선택", expanded=True):
        for option in mcp_config.MCP_OPTIONS:
            default_value = option in default_mcp_selections
            mcp_selections[option] = st.checkbox(
                option, key=f"mcp_{option}", value=default_value
            )

    if mcp_selections.get("사용자 설정"):
        mcp = mcp_config.load_user_defined_mcp()
        mcp_json_str = json.dumps(mcp, ensure_ascii=False, indent=2) if mcp else ""

        mcp_info = st.text_area(
            "MCP 설정을 JSON 형식으로 입력하세요 "
            "(Harness remote MCP: {\"mcpServers\": {\"name\": {\"url\": \"https://...\"}}} "
            "또는 {\"tools\": [...]})",
            value=mcp_json_str,
            height=150,
        )
        logger.info(f"mcp_info: {mcp_info}")

        if mcp_info:
            try:
                mcp_config.mcp_user_config = json.loads(mcp_info)
                logger.info(f"mcp_user_config: {mcp_config.mcp_user_config}")
                st.success("JSON 설정이 성공적으로 로드되었습니다.")
            except json.JSONDecodeError as e:
                st.error(f"JSON 파싱 오류: {str(e)}")
                st.error("올바른 JSON 형식으로 입력해주세요.")
                logger.error(f"JSON 파싱 오류: {str(e)}")
                mcp_config.mcp_user_config = {}
        else:
            mcp_config.mcp_user_config = {}

        mcp_config.save_user_defined_mcp(mcp_config.mcp_user_config)
        logger.info("save to user_defined_mcp.json")

    mcp_servers = [server for server, is_selected in mcp_selections.items() if is_selected]
    if (
        selected_skills != default_skill_selections
        or mcp_servers != default_mcp_selections
    ):
        utils.save_favorite_tools(skills=selected_skills, mcp_servers=mcp_servers)

    # model selection box
    modelName = st.selectbox(
        "🖊️ 사용 모델을 선택하세요",
        (
            "Claude 4.6 Sonnet",
            "Claude 5.0 Sonnet",
            "Claude 5.0 Opus",
            "Claude Fable 5",
    "Claude Fable 5.1",
            "Claude 4.7 Opus",
            "Claude 4.6 Opus",
            "Claude 4.5 Haiku",
            "Claude 4.5 Sonnet",
            "Claude 4.5 Opus",
            "OpenAI GPT 5.4",
            "OpenAI GPT 5.5",
            "OpenAI GPT 5.6 Sol",
            "OpenAI GPT 5.6 Terra",
            "OpenAI GPT 5.6 Luna",
            "OpenAI OSS 120B",
            "OpenAI OSS 20B",
            "Nova 2 Lite",
            "Nova Premier",
            "Nova Pro",
            "Nova Lite",
            "Nova Micro",
        ),
        index=0,
    )

    chat.update(modelName)
    st.success(f"Connected to {modelName}", icon="💚")

    st.subheader("📋 문서 업로드 (Knowledge Base)")
    uploaded_file = st.file_uploader(
        "RAG를 위한 파일을 선택합니다.",
        type=["pdf", "txt", "py", "md", "csv", "json", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "html", "png", "jpg", "jpeg"],
        key=chat.fileId,
    )

    clear_button = st.button("대화 초기화", key="clear")

st.title("🔮 " + mode)

if clear_button:
    chat.initiate()

# Initialize chat history
if "messages" not in st.session_state:
    st.session_state.messages = []
    st.session_state.greetings = False


def display_chat_messages() -> None:
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            if "images" in message:
                for url in message["images"]:
                    logger.info(f"url: {url}")
                    file_name = url[url.rfind("/") + 1 :]
                    st.image(url, caption=file_name, use_container_width=True)
            st.markdown(message["content"])


display_chat_messages()

if not st.session_state.greetings:
    with st.chat_message("assistant"):
        intro = (
            f"안녕하세요, `{st.session_state.user_id}`님. "
            "아마존 베드락을 이용하여 주셔서 감사합니다. "
            "왼쪽에서 Skill과 MCP를 선택한 뒤 대화를 시작하세요. "
            "문서를 업로드하면 Knowledge Base에 동기화됩니다."
        )
        st.markdown(intro)
        st.session_state.messages.append({"role": "assistant", "content": intro})
        st.session_state.greetings = True

if clear_button or "messages" not in st.session_state:
    st.session_state.messages = []
    st.session_state.greetings = False
    chat.initiate()
    st.rerun()

# Upload to S3 and sync Knowledge Base (agent-plugins pattern)
if uploaded_file is not None and clear_button is False:
    if uploaded_file.name:
        chat.initiate()
        file_name = uploaded_file.name
        logger.info(f"uploading... file_name: {file_name}")
        st.info(f'선택한 파일 "{file_name}"을 업로드합니다.')

        actor_id = chat.harness_actor_id()
        file_url = chat.upload_to_s3(
            uploaded_file.getvalue(), file_name, actor_id=actor_id
        )
        logger.info(f"file_url: {file_url} (actor_id={actor_id})")

        if not file_url:
            st.error(f'"{file_name}" S3 업로드에 실패했습니다.')
        else:
            sync_result = utils.sync_data_source()
            if sync_result:
                st.session_state.messages.append(
                    {
                        "role": "assistant",
                        "content": (
                            f'선택한 문서("{file_name}")를 S3에 업로드했고 '
                            f"Knowledge Base 동기화(ingestion)를 시작했습니다.\n\n"
                            f"- URL: {file_url}\n"
                            f"- job: {sync_result.get('ingestion_job_id', '')}\n"
                            f"- status: {sync_result.get('status', '')}"
                        ),
                    }
                )
                st.rerun()
            else:
                st.error(
                    f'"{file_name}" 업로드는 되었지만 Knowledge Base 동기화에 실패했습니다. '
                    "config.json의 knowledge_base_id / data_source_id를 확인하세요."
                )

if prompt := st.chat_input("메시지를 입력하세요."):
    with st.chat_message("user"):
        st.markdown(prompt)

    st.session_state.messages.append({"role": "user", "content": prompt})
    prompt = prompt.replace('"', "").replace("'", "")
    logger.info(f"prompt: {prompt}")

    with st.chat_message("assistant"):
        if mode == "Agent":
            chat.initiate()

            with st.status("thinking...", expanded=True, state="running") as status:
                notification_queue = NotificationQueue(container=status)

                skill_list = selected_skills if selected_skills else []
                logger.info(f"skill_list: {skill_list}")
                logger.info(f"mcp_servers: {mcp_servers}")

                response, image_url = agentcore_client.run_harness(
                    prompt,
                    notification_queue=notification_queue,
                    skill_list=skill_list,
                    mcp_servers=mcp_servers,
                )

            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": response,
                    "images": image_url if image_url else [],
                }
            )

            for url in image_url or []:
                logger.info(f"url: {url}")
                file_name = url[url.rfind("/") + 1 :]
                st.image(url, caption=file_name, use_container_width=True)


def main():
    pass


if __name__ == "__main__":
    pass
