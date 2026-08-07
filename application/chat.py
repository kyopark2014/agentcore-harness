import boto3
import uuid
import logging
import sys
import info
import utils
from urllib import parse

from langchain_aws import ChatBedrock
from botocore.config import Config

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("chat")

config = utils.load_config()
bedrock_region = config['region']
accountId = config['accountId']
projectName = config['projectName']
s3_bucket = config.get("s3_bucket") or utils.s3_bucket
s3_prefix = "docs"
s3_image_prefix = "images"
path = (config.get("sharing_url") or utils.sharing_url or "").rstrip("/")

model_name = "Claude 4.6 Sonnet"
model_type = "claude"
models = info.get_model_info(model_name)
model_id = models[0]["model_id"]

# runtime_session_id = str(uuid.uuid4())
runtime_session_id = "agentcore"
logger.info(f"runtime_session_id: {runtime_session_id}")
user_id = None
fileId = uuid.uuid4().hex

def initiate():
    global runtime_session_id, fileId
    runtime_session_id=str(uuid.uuid4())
    fileId = uuid.uuid4().hex
    logger.info(f"runtime_session_id: {runtime_session_id}")

debug_mode = 'Disable'

def update(modelName):
    global model_name, models, model_type, model_id

    if modelName != model_name:
        model_name = modelName
        logger.info(f"modelName: {modelName}")

        models = info.get_model_info(model_name)
        if not models:
            logger.error(f"Unknown model: {modelName}")
            return
        model_type = models[0]["model_type"]
        model_id = models[0]["model_id"]
        logger.info(f"model_id: {model_id}")
        logger.info(f"model_type: {model_type}")


def harness_model_config() -> dict:
    """Build InvokeHarness ``model`` override from the selected chat profile."""
    profile = models[0] if models else {}
    mid = profile.get("model_id") or model_id
    bedrock_cfg: dict = {"modelId": mid}
    api_format = profile.get("mantle_api") or profile.get("apiFormat")
    if api_format:
        bedrock_cfg["apiFormat"] = api_format
    return {"bedrockModelConfig": bedrock_cfg}


def upload_to_s3(file_bytes, file_name):
    """Upload a file to S3 under docs/ or images/ and return the sharing URL."""
    try:
        s3_client = boto3.client(
            service_name="s3",
            region_name=bedrock_region,
        )

        content_type = utils.get_contents_type(file_name)
        logger.info(f"content_type: {content_type}")

        if content_type.startswith("image/"):
            s3_key = f"{s3_image_prefix}/{file_name}"
            url_path = f"{s3_image_prefix}/{parse.quote(file_name)}"
        else:
            s3_key = f"{s3_prefix}/{file_name}"
            url_path = f"{s3_prefix}/{parse.quote(file_name)}"

        put_params = {
            "Bucket": s3_bucket,
            "Key": s3_key,
            "Metadata": {
                "content_type": content_type,
                "model_name": model_name,
            },
            "Body": file_bytes,
        }
        if content_type != "no info":
            put_params["ContentType"] = content_type

        response = s3_client.put_object(**put_params)
        logger.info(f"upload response: {response}")

        if path:
            return f"{path}/{url_path}"
        return f"s3://{s3_bucket}/{s3_key}"

    except Exception as e:
        err_msg = f"Error uploading to S3: {str(e)}"
        logger.info(f"{err_msg}")
        return None


def get_chat(extended_thinking=None):
    # Set default value if not provided or invalid
    if extended_thinking is None or extended_thinking not in ['Enable', 'Disable']:
        extended_thinking = 'Disable'

    logger.info(f"model_name: {model_name}")
    profile = models[0]
    bedrock_region =  profile['bedrock_region']
    modelId = profile['model_id']
    model_type = profile['model_type']
    maxOutputTokens = 4096 # 4k
    logger.info(f"LLM: bedrock_region: {bedrock_region}, modelId: {modelId}, model_type: {model_type}")

    STOP_SEQUENCE = "\n\nHuman:" 
                          
    # bedrock   
    boto3_bedrock = boto3.client(
        service_name='bedrock-runtime',
        region_name=bedrock_region,
        config=Config(
            retries = {
                'max_attempts': 30
            }
        )
    )
    
    if extended_thinking=='Enable':
        maxReasoningOutputTokens=64000
        logger.info(f"extended_thinking: {extended_thinking}")
        thinking_budget = min(maxOutputTokens, maxReasoningOutputTokens-1000)

        parameters = {
            "max_tokens":maxReasoningOutputTokens,
            "temperature":1,            
            "thinking": {
                "type": "enabled",
                "budget_tokens": thinking_budget
            },
            "stop_sequences": [STOP_SEQUENCE]
        }
    else:
        parameters = {
            "max_tokens":maxOutputTokens,     
            "temperature":0.1,
            "top_k":250,
            "stop_sequences": [STOP_SEQUENCE]
        }

    chat = ChatBedrock(   # new chat model
        model_id=modelId,
        client=boto3_bedrock, 
        model_kwargs=parameters,
        region_name=bedrock_region
    )    
    return chat

