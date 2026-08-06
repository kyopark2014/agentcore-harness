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

with st.sidebar:
    st.title("🔮 Menu")

    st.markdown(
        "Amazon의 AgentCore Harness를 이용해 Agent를 구현합니다. "
        "여기에서는 SKILL과 MCP를 이용해 agent의 기능을 확장합니다.\n"
        "상세한 코드는 [Github](https://github.com/kyopark2014/agentcore-harness)을 참조하세요."
    )

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
            "아마존 베드락을 이용하여 주셔서 감사합니다. "
            "왼쪽에서 Skill과 MCP를 선택한 뒤 대화를 시작하세요."
        )
        st.markdown(intro)
        st.session_state.messages.append({"role": "assistant", "content": intro})
        st.session_state.greetings = True

if clear_button or "messages" not in st.session_state:
    st.session_state.messages = []
    st.session_state.greetings = False
    chat.initiate()
    st.rerun()

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
