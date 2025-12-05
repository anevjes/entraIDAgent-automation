# filepath: Direct OpenAI compatible approach
import argparse
from openai import OpenAI 
from azure.identity import DefaultAzureCredential, InteractiveBrowserCredential, get_bearer_token_provider 

parser = argparse.ArgumentParser()
parser.add_argument("--interactive", action="store_true", help="Use interactive browser login")
args = parser.parse_args()

if args.interactive:
    credential = InteractiveBrowserCredential()
else:
    credential = DefaultAzureCredential()

# edit base_url with your <foundry-resource-name>, <project-name>, and <app-name>
openai = OpenAI(
    api_key=get_bearer_token_provider(credential, "https://ai.azure.com/.default"),
    base_url="https://wds-aif-001.services.ai.azure.com/api/projects/demo1/applications/fsdhsdf/protocols/openai",
    default_query = {"api-version": "2025-11-15-preview"}
)

response = openai.responses.create( 
  input="tell me about honda integra type r?", 
) 
print(f"Response output: {response.output_text}")