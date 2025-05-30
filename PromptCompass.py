import json
import streamlit as st
import streamlit_ext as ste
import os
import time
import gc
import pandas as pd
import traceback
from pathlib import Path
from dotenv import load_dotenv
from langchain.chains import LLMChain  # import LangChain libraries
from langchain.llms import OpenAI  # import OpenAI model
from langchain.chat_models import ChatOpenAI  # import OpenAI chat model
from langchain.callbacks import get_openai_callback  # import OpenAI callbacks
from langchain.prompts import PromptTemplate  # import PromptTemplate
from langchain.llms import HuggingFacePipeline  # import HuggingFacePipeline
from langchain_anthropic import ChatAnthropic  # import Anthropic model
import torch  # import torch
# pip install git+https://github.com/huggingface/transformers
from transformers import AutoTokenizer, pipeline, AutoModelForSeq2SeqLM, AutoModelForCausalLM, StoppingCriteria, StoppingCriteriaList

os.environ['CUDA_LAUNCH_BLOCKING'] = '1'


def prime_gpu_with_dummy_model():
    """
    Loads and unloads a small dummy model using session_state and standard unload functions.
    
    WHY? Because the FIRST model can cause memory to be allocated and not released properly. Additonal models seem 
    to load and unload without issue. I cannot sort out why and even attempted to force devices. If a very large
    model is loaded first, future models will not even use the GPU.

    """
    if os.environ.get('STREAMLIT_GPU_PRIMED') == 'true':
        return

    # Mark that this process is now executing the priming logic.
    os.environ['STREAMLIT_GPU_PRIMED'] = 'true'

    st.write("Attempting one-time GPU priming with a small Llama model using session_state...")
    dummy_model_id = "google/flan-t5-small"
    original_model_id_in_state = st.session_state.get('current_model_id') # Save if something was there

    # Temporarily set current_model_id for the priming operation if needed by unload logic
    st.session_state['current_model_id'] = dummy_model_id 

    try:
        st.write(f"Priming: Loading {dummy_model_id}...")

        st.session_state['tokenizer'] = AutoTokenizer.from_pretrained(dummy_model_id)
        st.session_state['model_instance'] = None
        # AutoModelForCausalLM.from_pretrained(
        #     dummy_model_id,
        #     torch_dtype="auto",
        #     device_map="auto"
        # )
        st.write(f"Priming: {dummy_model_id} model instance loaded.")

        st.session_state['model_pipe'] = pipeline(
            task='text-generation',
            model=dummy_model_id,
            tokenizer=st.session_state['tokenizer'],
            torch_dtype="auto", # Model's dtype should be respected
            device_map="auto",
            num_return_sequences=1,
            eos_token_id=st.session_state['tokenizer'].eos_token_id,
            return_full_text=True
        )
        st.write(f"Priming: Pipeline for {dummy_model_id} created. Pipeline device: {st.session_state['model_pipe'].device}")

        st.session_state['local_llm'] = HuggingFacePipeline(
            pipeline=st.session_state['model_pipe']
        )

        # Create a dummy prompt and chain for completeness if unload_model_completely expects it
        dummy_template = PromptTemplate(input_variables=["user_input"], template="Priming: {user_input}")
        st.session_state['llm_chain'] = LLMChain(
            llm=st.session_state['local_llm'], prompt=dummy_template
        )
        st.write(f"Priming: Dummy LLMChain created for {dummy_model_id}.")

        # # Optionally, a tiny inference to ensure full initialization
        st.session_state['llm_chain'].run("hello")
        output = st.session_state['llm_chain'].run("hello")
        st.write(f"Priming: Dummy inference complete for {dummy_model_id}.")

        st.session_state.gpu_primed_this_session = True # Mark as success before unload
        st.write(f"Priming: {dummy_model_id} fully loaded into session_state.")

    except Exception as e:
        st.error(f"Error during GPU priming with {dummy_model_id}: {e}")
        st.session_state.gpu_primed_this_session = False # Mark as failed
        # Clean up whatever might have been partially loaded into session_state
        unload_model_completely(st.session_state) 
        force_cuda_release() # Pass context if your function signature was updated
    finally:
        st.write(f"Priming: Unloading {dummy_model_id} using standard functions...")
        # Call the standard unload functions which will use st.session_state
        unload_model_completely(st.session_state)
        force_cuda_release() # Pass context if your function signature was updated
        
        # Restore original current_model_id if it was saved
        if original_model_id_in_state:
            st.session_state['current_model_id'] = original_model_id_in_state
        elif 'current_model_id' in st.session_state: # If priming set it and there was no original
            st.session_state.pop('current_model_id', None)

        if st.session_state.get('gpu_primed_this_session', False):
            st.write("Priming: Dummy model unloaded and CUDA cache cleared via standard functions.")
        else:
            st.write("Priming: Cleanup attempted after priming failure.")

def unload_model_completely(session_state):
    """Unload model by breaking all reference cycles before deletion"""
    llm_chain_obj = session_state.pop('llm_chain', None)
    if llm_chain_obj:
        if hasattr(llm_chain_obj, 'llm'): llm_chain_obj.llm = None
        if hasattr(llm_chain_obj, 'prompt'): llm_chain_obj.prompt = None
        del llm_chain_obj

    local_llm_obj = session_state.pop('local_llm', None)
    if local_llm_obj:
        if hasattr(local_llm_obj, 'pipeline'): local_llm_obj.pipeline = None
        del local_llm_obj
    
    model_pipe_obj = session_state.pop('model_pipe', None)
    if model_pipe_obj:
        # Break references the pipeline might hold
        if hasattr(model_pipe_obj, 'model'): model_pipe_obj.model = None
        if hasattr(model_pipe_obj, '_model'): model_pipe_obj._model = None # Some pipelines use _model
        if hasattr(model_pipe_obj, 'tokenizer'): model_pipe_obj.tokenizer = None
        if hasattr(model_pipe_obj, 'device'): model_pipe_obj.device = None
        del model_pipe_obj

    # Handle model_instance
    model_instance_obj = session_state.pop('model_instance', None)
    if model_instance_obj:
        try:
            # Attempt to move to CPU first
            if hasattr(model_instance_obj, 'to') and hasattr(model_instance_obj, 'device') and model_instance_obj.device.type != 'cpu':
                model_instance_obj.to('cpu')
                if torch.cuda.is_available():
                    torch.cuda.synchronize() # Ensure move completes
        except Exception as e:
            st.warning(f"Failed to move model_instance to CPU during unload: {e}")
        
        # Clear internal modules if it's an nn.Module
        if hasattr(model_instance_obj, '_modules'):
            for key in list(model_instance_obj._modules.keys()):
                model_instance_obj._modules[key] = None
        del model_instance_obj

    tokenizer_obj = session_state.pop('tokenizer', None)
    if tokenizer_obj:
        del tokenizer_obj

    # Force garbage collection to break reference cycles
    gc.collect()

def force_cuda_release():
    """Force aggressive CUDA memory release for a specific model type"""
    if not torch.cuda.is_available():
        return
    try:
        gc.collect() 
        torch.cuda.empty_cache()
        torch.cuda.synchronize() 
    except Exception as e:
        st.warning(f"Advanced CUDA cleanup failed: {e}")

def main():    
    load_dotenv(".env")

    if 'gpu_primed_this_session' not in st.session_state:
        st.session_state.gpu_primed_this_session = False # Initialize if not present
    if not st.session_state.gpu_primed_this_session:
        prime_gpu_with_dummy_model()

    open_ai_key = None
    claude_api_key = None
    uploaded_file = None

    # import css tasks and prompts
    with open('prompts.json') as f:
        promptlib = json.load(f)

    hide_default_format = """
       <style>
       #MainMenu {visibility: hidden; }
       footer {visibility: hidden;}
       </style>
       """
    st.markdown(hide_default_format, unsafe_allow_html=True)

    # title
    st.title("Prompt Compass")
    st.subheader(
        "A Tool for Navigating LLMs and Prompts for Computational Social Science and Digital Humanities Research")
    # Add Link to your repo
    st.markdown(
        '''
        [![Repo](https://badgen.net/badge/icon/GitHub?icon=github&label)](https://github.com/ErikBorra/PromptCompass)
        [![DOI](https://zenodo.org/badge/649855474.svg)](https://zenodo.org/badge/latestdoi/649855474)
        ''', unsafe_allow_html=True)


    if Path("intro.md").exists():
        with Path("intro.md").open() as infile:
            intro = infile.read()
        if intro.strip():
            st.markdown(intro)

    # load available models
    model_with_names = [
        model for model in promptlib['models'] if model['name']]
    # create input area for model selection
    input_values = {}
    input_values['model'] = st.selectbox('Select a model', model_with_names,
                                         format_func=lambda x: '(Hosted by ' + x.get('owner', 'Digital Methods Initiative') + ') ' + x['name'])    # Check if model has changed and unload previous model if necessary
    current_model_id = input_values['model']['name']
    previous_model_id = st.session_state.get('current_model_id')
    if previous_model_id and previous_model_id != current_model_id:
        # Use our complete unloading function
        unload_model_completely(st.session_state)
        st.info(f"Unloaded previous model: {previous_model_id}")
    
    # Update the session state
    st.session_state['current_model_id'] = current_model_id

    st.caption(f"Model info: [{input_values['model']['name']}]({input_values['model']['resource']})" + (
        f". {input_values['model']['comment']}" if 'comment' in input_values['model'] else ""))

    # ask for open ai key if no key is set in .env
    if input_values['model']['resource'] in ["https://platform.openai.com/docs/models/gpt-3-5", "https://platform.openai.com/docs/models/gpt-4", "https://platform.openai.com/docs/models/gpt-4o", "https://platform.openai.com/docs/models/gpt-4o-mini","https://platform.openai.com/docs/models#o1"]:
        # Load the OpenAI API key from the environment variable
        if os.getenv("OPENAI_API_KEY") is None or os.getenv("OPENAI_API_KEY") == "":
            open_ai_key = st.text_input("OpenAI API Key", "")
        else:
            open_ai_key = os.getenv("OPENAI_API_KEY")

    # ask for open claude key if no key is set in .env
    if input_values['model']['resource'] in ["https://docs.anthropic.com/en/docs/about-claude/models"]:
        # Load the Claude API key from the environment variable
        if os.getenv("CLAUDE_API_KEY") is None or os.getenv("CLAUDE_API_KEY") == "":
            claude_api_key = st.text_input("Claude API Key", "")
        else:
            claude_api_key = os.getenv("CLAUDE_API_KEY")

    # set default values
    do_sample = False
    temperature = 0.001
    top_p = -1
    max_new_tokens = -1
    with st.expander("Advanced settings"):
        if input_values['model']['resource'] not in ["https://platform.openai.com/docs/models/gpt-3-5", "https://platform.openai.com/docs/models/gpt-4", "https://platform.openai.com/docs/models/gpt-4o", "https://platform.openai.com/docs/models/gpt-4o-mini","https://platform.openai.com/docs/models#o1"]:
            st.markdown(
                """
            **Set Maximum Length**: Determines the maximum number of tokens of the **generated** text. A token is approximately four characters word, although this depends on the model.
            A value of -1 means the parameter will not be specified.
            """
            )
            max_new_tokens = st.number_input(
                'Maximum Length', value=1024, min_value=-1, step=1)
            st.markdown(
                """
            **Set do_sample**: This controls how the model generates text. If do_sample=True, the model will use a probabilistic approach to generate text, where the likelihood of each word being chosen depends on its predicted probability. Use the below parameters to further control its behavior. If do_sample=False, the model will use a deterministic approach and always choose the most likely next word.
            """
            )
            do_sample = st.radio(
                'Set do_sample',
                ('False', 'True')
            )
        st.markdown(
            """
        **Temperature**: Controls the randomness in the model's responses.
        Lower values (closer to 0.0) make the output more deterministic, while higher values (closer to 2.0) make it more diverse.
        A value of -1 means the parameter will not be specified.
        """
        )
        temperature = st.number_input(
            'Set Temperature', min_value=-1.0, max_value=2.0, value=0.001, format="%.3f")

        st.markdown(
            """
        **Top P**: Also known as "nucleus sampling", is an alternative to temperature that can also be used to control the randomness of the model's responses.
        It essentially trims the less likely options in the model's distribution of possible responses. Possible values lie between 0.0 and 1.0.
        A value of -1 means the parameter will not be specified. Only applies if do_sample=True.
        """
        )
        top_p = st.number_input('Set Top-P', min_value=-
                                1.0, max_value=1.0, value=-1.0)

    # Check for correct values
    allgood = True
    # set model kwargs
    model_kwargs = {}

    if input_values['model']['resource'] not in ["https://platform.openai.com/docs/models/gpt-3-5", "https://platform.openai.com/docs/models/gpt-4", "https://platform.openai.com/docs/models/gpt-4o", "https://platform.openai.com/docs/models/gpt-4o-mini", "https://platform.openai.com/docs/models#o1","https://docs.anthropic.com/en/docs/about-claude/models"]:
        # check if max_new_tokens is at least 1 or -1
        if not (max_new_tokens > 0 or max_new_tokens == -1):
            st.error(
                'Error: Max Tokens must be at least 1. Choose -1 if you want to use the default model value.')
            max_new_tokens = -1
            allgood = False
        if max_new_tokens > 0:
            model_kwargs['max_new_tokens'] = max_new_tokens

        if do_sample not in ['True', 'False']:
            st.error(
                'Error: do_Sample must be True or False')
            do_sample = False
            allgood = False
        do_sample = True if do_sample == 'True' else False
        if do_sample in [True, False]:
            model_kwargs['do_sample'] = do_sample

    if not (0 <= temperature <= 2 or temperature == -1):
        st.error(
            "Temperature value must be between 0 and 2. Choose -1 if you want to use the default model value.")
        temperature = -1
        allgood = False
    if 0 <= temperature <= 2:
        model_kwargs['temperature'] = temperature
    if not (0 <= top_p <= 1 or top_p == -1):
        st.error(
            "Top P value must be between 0 and 1. Choose -1 if you want to use the default model value.")
        top_p = -1
        allgood = False
    if 0 <= top_p <= 1:
        model_kwargs['top_p'] = top_p

    # create input area for task selection
    tasks_with_names = [task for task in promptlib['tasks'] if task['name']]
    task = st.selectbox('Select a task', tasks_with_names,
                        format_func=lambda x: x['name'] + " - " + x['authors'])

    # Create input areas for prompts and user input
    if task:

        # concatenate all strings from prompt array
        prompt = '\n'.join(task['prompt'])

        # create input area for prompt
        input_values['prompt'] = st.text_area(
            "Inspect, and possibly modify, the prompt by ["+task['authors']+"]("+task['paper']+")", prompt, height=200)

        # allow the user to select the input type
        input_type = st.radio("Choose input type:",
                              ('Text input', 'Upload a CSV'), horizontal=True)

        if input_type == 'Text input':
            # create input area for user input
            input_values['user'] = st.text_area(
                "Input to be analyzed with the prompt (one thing per line):",
                "this user is happy\none user is just a user\nthe other user is a lier")
            # if the user's input is not a list (e.g. a string), then split it by newlines
            if isinstance(input_values['user'], str):
                input_values['user'] = input_values['user'].split('\n')
            original_data = pd.DataFrame(
                input_values['user'], columns=['user_input'])
        else:
            # upload CSV
            uploaded_file = st.file_uploader("Choose a CSV file (only the first 150 items will be processed)", type="csv")

            if uploaded_file is not None:
                # convert the uploaded file to a dataframe
                original_data = pd.read_csv(uploaded_file, nrows=150)

                # ask user to select a column
                column_to_extract = st.selectbox(
                    'Choose a column to apply the prompt on:', original_data.columns)

                # process the selected column from the dataframe
                input_values['user'] = original_data[column_to_extract].tolist()

        data = pd.DataFrame()

        # Determine the output file name
        filename = uploaded_file.name if uploaded_file else 'output.csv'
        base_filename, file_extension = os.path.splitext(filename)
        output_filename = f"{base_filename}_promptcompass{file_extension}"

    repeat_input = st.number_input(
        'Enter the number of times the prompt/input combination should be repeated:', min_value=1, max_value=3, value=1, step=1)

    # Submit button
    submit_button = st.button('Submit')

    st.write('---')  # Add a horizontal line

    # Process form submission
    if submit_button and allgood:
        if 'user' not in input_values or input_values['user'] is None:
            st.error("No user input provided")

        else:
            with st.spinner(text="In progress..."):

                try:

                    start_time = time.time()
                    st.write("Start time: " +
                             time.strftime("%H:%M:%S", time.localtime()))

                    if input_values['prompt'] and input_values['user']:

                        # create prompt template
                        # add location of user input to prompt
                        if task['location_of_input'] == 'before':
                            template = "{user_input}" + \
                                "\n\n" + input_values['prompt']
                        elif task['location_of_input'] == 'after':
                            template = input_values['prompt'] + \
                                "\n\n" + "{user_input}"
                        else:
                            template = input_values['prompt']

                        # make sure users don't forget the user input variable
                        if "{user_input}" not in template:
                            template = template + "\n\n{user_input}"

                        # fill prompt template
                        prompt_template = PromptTemplate(
                            input_variables=["user_input"], template=template)

                        # loop over user values in prompt
                        for key, user_input in enumerate(input_values['user']):

                            for i in range(repeat_input):

                                num_prompt_tokens = None
                                num_completion_tokens = None
                                cost = None

                                user_input = str(user_input).strip()
                                if user_input == "" or user_input == "nan":
                                    continue

                                # set up and run the model
                                model_id = input_values['model']['name']
                                if model_id in ["claude-3-5-sonnet-latest", "claude-3-5-haiku-latest", "claude-3-opus-latest", "claude-3-sonnet-latest", "claude-3-haiku-latest"]:
                                    if claude_api_key is None or claude_api_key == "":
                                        st.error(
                                            "Please provide a Claude API Key")
                                        exit(1)
                                    if model_id in ["claude-3-5-sonnet-latest", "claude-3-5-haiku-latest", "claude-3-opus-latest", "claude-3-sonnet-latest", "claude-3-haiku-latest"]:
                                        llm = ChatAnthropic(
                                            model=model_id, api_key=claude_api_key, **model_kwargs)

                                        AIMessage = llm.invoke(prompt_template.format(
                                            user_input=user_input))
                                        output = AIMessage.content
                                        num_completion_tokens = AIMessage.usage_metadata[
                                            'output_tokens']
                                        num_prompt_tokens = AIMessage.usage_metadata['input_tokens']

                                        st.success("Input:  " + user_input + "  \n\n " +
                                                   "Output: " + output)

                                elif model_id in ['gpt-3.5-turbo', "gpt-3.5-turbo-instruct", 'gpt-4', 'gpt-4-turbo', "gpt-4o-mini", "gpt-4o", "o1","o1-mini","o1-preview",'babbage-002', 'davinci-002']:
                                    if open_ai_key is None or open_ai_key == "":
                                        st.error(
                                            "Please provide an Open AI API Key")
                                        exit(1)
                                    with get_openai_callback() as cb:
                                        if model_id in ['gpt-3.5-turbo', "gpt-3.5-turbo-instruct", 'gpt-4', 'gpt-4-turbo', "gpt-4o-mini", "gpt-4o","o1","o1-mini","o1-preview"]:
                                            llm = ChatOpenAI(
                                                model=model_id, openai_api_key=open_ai_key, **model_kwargs)
                                        else:
                                            llm = OpenAI(
                                                model=model_id, openai_api_key=open_ai_key, **model_kwargs)

                                        st.session_state['llm_chain'] = LLMChain(
                                            llm=llm, prompt=prompt_template)

                                        output = st.session_state['llm_chain'].run(user_input)

                                        st.success("Input:  " + user_input + "  \n\n " +
                                                   "Output: " + output)
                                        st.text(cb)
                                        num_prompt_tokens = cb.prompt_tokens
                                        num_completion_tokens = cb.completion_tokens
                                        cost = cb.total_cost

                                elif model_id.startswith("deepseek") or model_id in ['meta-llama/Llama-2-7b-chat-hf', 'meta-llama/Llama-2-13b-chat-hf', 'meta-llama/Meta-Llama-3-8B', 'meta-llama/Meta-Llama-3.1-8B', 'meta-llama/Meta-Llama-3-8B-Instruct', 'meta-llama/Meta-Llama-3.1-8B-Instruct']:
                                    if st.session_state.get('model_pipe') is None:
                                        with st.status('Loading model %s' % model_id) as status:
                                            # to use the llama-2 models,
                                            # you first need to get access to the llama-2 models via e.g. https://huggingface.co/meta-llama/Llama-2-7b-chat-hf
                                            # once accepted, get a hugging face auth token https://huggingface.co/settings/tokens
                                            # and then run `huggingface-cli login` on the command line, filling in the generated token
                                            if model_id in ['meta-llama/Llama-2-7b-chat-hf', 'meta-llama/Llama-2-13b-chat-hf', 'meta-llama/Meta-Llama-3-8B', 'meta-llama/Meta-Llama-3.1-8B', 'meta-llama/Meta-Llama-3-8B-Instruct', 'meta-llama/Meta-Llama-3.1-8B-Instruct']:
                                                st.session_state['tokenizer'] = AutoTokenizer.from_pretrained(
                                                    model_id, token=True)
                                            else:
                                                st.session_state['tokenizer'] = AutoTokenizer.from_pretrained(
                                                    model_id)

                                            if model_id == "meta-llama/Llama-2-13b-chat-hf":
                                                st.session_state['model_pipe'] = pipeline(
                                                    "text-generation",
                                                    model=model_id,
                                                    tokenizer=st.session_state['tokenizer'],
                                                    # torch_dtype="auto",
                                                    trust_remote_code=True,
                                                    device_map="auto",
                                                    num_return_sequences=1,
                                                    eos_token_id=st.session_state['tokenizer'].eos_token_id,
                                                    **model_kwargs
                                                )
                                            else:
                                                st.session_state['model_pipe'] = pipeline(
                                                    "text-generation",
                                                    model=model_id,
                                                    tokenizer=st.session_state['tokenizer'],
                                                    torch_dtype="auto",
                                                    trust_remote_code=True,
                                                    device_map="auto",
                                                    num_return_sequences=1,
                                                    eos_token_id=st.session_state['tokenizer'].eos_token_id,
                                                    **model_kwargs
                                                )
                                            
                                            st.session_state['local_llm'] = HuggingFacePipeline(
                                                pipeline=st.session_state['model_pipe'])

                                        status.update(
                                            label='Model %s loaded' % model_id, state="complete")

                                    st.session_state['llm_chain'] = LLMChain(
                                        llm=st.session_state['local_llm'], prompt=prompt_template)

                                    output = st.session_state['llm_chain'].run(user_input)[len(prompt_template.format(user_input=user_input)):]

                                    # this is for deepseek - remove the "internal thoughts" from the output
                                    if "</think>" in output:
                                        output = output.split("</think>")[-1].strip()

                                    st.success("Input:  " + user_input + "  \n\n " +
                                               "Output: " + output)
                                    
                                elif model_id in ['google/flan-t5-large', 'google/flan-t5-xl', 'google/gemma-2b-it', 'google/gemma-7b-it', 'tiiuae/falcon-7b-instruct', 'tiiuae/falcon-40b-instruct', 'databricks/dolly-v2-3b', 'databricks/dolly-v2-7b']:
                                    if st.session_state.get('model_pipe') is None:
                                        with st.status('Loading model %s' % model_id) as status:
                                            st.session_state['tokenizer'] = AutoTokenizer.from_pretrained(
                                                model_id)

                                            if model_id in ['google/flan-t5-large', 'google/flan-t5-xl']:
                                                st.session_state['model_instance']  = AutoModelForSeq2SeqLM.from_pretrained( 
                                                    model_id, 
                                                    load_in_8bit=False, 
                                                    device_map="auto",
                                                    )
                                                
                                                st.session_state['model_pipe'] = pipeline(
                                                    "text2text-generation",
                                                    model=st.session_state['model_instance'],
                                                    tokenizer=st.session_state['tokenizer'],
                                                    # torch_dtype="auto",
                                                    trust_remote_code=True,
                                                    device_map="auto",
                                                    num_return_sequences=1,
                                                    eos_token_id=st.session_state['tokenizer'].eos_token_id,
                                                    **model_kwargs
                                                )
                                            # elif model_id in ['tiiuae/falcon-7b-instruct', 'tiiuae/falcon-40b-instruct']:
                                            else:
                                                st.session_state['model_pipe'] = pipeline(
                                                    "text-generation",
                                                    model=model_id,
                                                    tokenizer=st.session_state['tokenizer'],
                                                    torch_dtype="auto",
                                                    trust_remote_code=True,
                                                    device_map="auto",
                                                    num_return_sequences=1,
                                                    eos_token_id=st.session_state['tokenizer'].eos_token_id,
                                                    **model_kwargs
                                                )

                                            st.session_state['local_llm'] = HuggingFacePipeline(
                                                pipeline=st.session_state['model_pipe'])
                                            status.update(
                                                label='Model %s loaded' % model_id, state="complete")

                                    st.session_state['llm_chain'] = LLMChain(
                                        llm=st.session_state['local_llm'], prompt=prompt_template)

                                    output = st.session_state['llm_chain'].run(user_input)

                                    st.success("Input:  " + user_input + "  \n\n " +
                                               "Output: " + output)
                                                              
                                elif model_id == "mosaicml/mpt-7b-instruct":
                                    if st.session_state.get('model_pipe') is None:
                                        with st.status('Loading model %s' % model_id) as status:                                            
                                            st.session_state['model_instance'] = AutoModelForCausalLM.from_pretrained(
                                                model_id,
                                                trust_remote_code=True,
                                                torch_dtype=torch.bfloat16,
                                                max_seq_len=2048,
                                                device_map="auto"
                                            )

                                            # MPT-7B model was trained using the EleutherAI/gpt-neox-20b tokenizer
                                            st.session_state['tokenizer'] = AutoTokenizer.from_pretrained(
                                                "EleutherAI/gpt-neox-20b")

                                            # mtp-7b is trained to add "<|endoftext|>" at the end of generations
                                            stop_token_ids = st.session_state['tokenizer'].convert_tokens_to_ids(
                                                ["<|endoftext|>"])

                                            # define custom stopping criteria object
                                            class StopOnTokens(StoppingCriteria):
                                                def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor, **kwargs) -> bool:
                                                    for stop_id in stop_token_ids:
                                                        if input_ids[0][-1] == stop_id:
                                                            return True
                                                    return False
                                            stopping_criteria = StoppingCriteriaList(
                                                [StopOnTokens()])
                                            
                                            st.session_state['model_pipe'] = pipeline(
                                                task='text-generation',
                                                model=st.session_state['model_instance'],
                                                tokenizer=st.session_state['tokenizer'],
                                                torch_dtype="auto",
                                                device_map="auto",
                                                num_return_sequences=1,
                                                eos_token_id=st.session_state['tokenizer'].eos_token_id,
                                                **model_kwargs,
                                                return_full_text=True,  # langchain expects the full text
                                                stopping_criteria=stopping_criteria,  # without this model will ramble
                                                repetition_penalty=1.1  # without this output begins repeating
                                            )

                                            st.session_state['local_llm'] = HuggingFacePipeline(
                                                pipeline=st.session_state['model_pipe'])

                                        status.update(
                                            label='Model %s loaded' % model_id, state="complete")

                                    st.session_state['llm_chain'] = LLMChain(
                                        llm=st.session_state['local_llm'], prompt=prompt_template)

                                    output = st.session_state['llm_chain'].run(user_input)

                                    st.success("Input:  " + user_input + "  \n\n " +
                                               "Output: " + output)
                                
                                elif model_id == "allenai/OLMo-7B" or model_id == "ehartford/dolphin-2.1-mistral-7b" or model_id == "lvkaokao/mistral-7b-finetuned-orca-dpo-v2" or model_id == "lmsys/vicuna-13b-v1.5" or model_id == "microsoft/Orca-2-13b":
                                    if st.session_state.get('model_pipe') is None:
                                        with st.status('Loading model %s' % model_id) as status:
                                            st.session_state['model_instance'] = AutoModelForCausalLM.from_pretrained(
                                                model_id,
                                                trust_remote_code=True,
                                                torch_dtype=torch.bfloat16,
                                                device_map="auto"
                                            )

                                            if model_id == "ehartford/dolphin-2.1-mistral-7b":
                                                st.session_state['tokenizer'] = AutoTokenizer.from_pretrained(
                                                    model_id, use_fast=False)
                                            else:
                                                st.session_state['tokenizer'] = AutoTokenizer.from_pretrained(
                                                    model_id)

                                            st.session_state['model_pipe'] = pipeline(
                                                task='text-generation',
                                                model=st.session_state['model_instance'],
                                                tokenizer=st.session_state['tokenizer'],
                                                torch_dtype="auto",
                                                device_map="auto",
                                                num_return_sequences=1,
                                                eos_token_id=st.session_state['tokenizer'].eos_token_id,
                                                **model_kwargs,
                                                return_full_text=True,  # langchain expects the full text
                                            )

                                            st.session_state['local_llm'] = HuggingFacePipeline(
                                                pipeline=st.session_state['model_pipe'])

                                        status.update(
                                            label='Model %s loaded' % model_id, state="complete")

                                    st.session_state['llm_chain'] = LLMChain(
                                        llm=st.session_state['local_llm'], prompt=prompt_template)

                                    output = st.session_state['llm_chain'].run(user_input)

                                    st.success("Input:  " + user_input + "  \n\n " +
                                               "Output: " + output)
                                
                                else:
                                    st.error("Model %s not found" % model_id)
                                    exit(1)
                                    
                                if not num_prompt_tokens or not num_completion_tokens:
                                    num_prompt_tokens = len(st.session_state['tokenizer'].tokenize(
                                        prompt_template.format(user_input=user_input)))
                                    num_completion_tokens = len(st.session_state['tokenizer'].tokenize(
                                        output))

                                # Prepare data as dictionary
                                original_row = original_data.loc[key].copy()

                                new_row = {
                                    'user_input': user_input,
                                    'output': output,
                                    'llm': model_id,
                                    'prompt name': task['name'],
                                    'prompt authors': task['authors'],
                                    'prompt': template,
                                    'timestamp': time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                                    '# prompt tokens': str(int(num_prompt_tokens)),
                                    '# completion tokens': str(int(num_completion_tokens)),
                                    'max_new_tokens': int(model_kwargs['max_new_tokens']) if "max_new_tokens" in model_kwargs else None,
                                    'do_sample': int(model_kwargs['do_sample']) if "do_sample" in model_kwargs else None,
                                    'temperature': model_kwargs['temperature'] if "temperature" in model_kwargs else None,
                                    'top_p': model_kwargs['top_p'] if "top_p" in model_kwargs else None,
                                    'cost': cost if cost is not None else None
                                }

                                # Update the original row with the new data
                                for key2, value in new_row.items():
                                    original_row[key2] = value

                                # Append the updated row to the DataFrame
                                updated_row_df = pd.DataFrame([original_row])
                                data = pd.concat(
                                    [data, updated_row_df], ignore_index=True)

                        st.subheader("Results")
                        st.dataframe(data, column_config={},
                                     hide_index=True)

                        # make output available as csv
                        csv = data.to_csv(index=False).encode('utf-8')
                        ste.download_button(
                            "Download CSV",
                            csv,
                            output_filename,
                            "text/csv",
                        )

                    end_time = time.time()
                    elapsed_time = end_time - start_time
                    st.write("End time: " +
                             time.strftime("%H:%M:%S", time.localtime()))
                    st.write("Elapsed time: " +
                             str(round(elapsed_time, 2)) + " seconds")                
                
                except Exception as e:
                    t = traceback.format_exception(e)
                    st.error(f"Exception encountered: {type(e).__name__} / {e}<br><br>{'<br>'.join(t)}")
                
                finally:
                    # Explicitly delete objects that hold references to session_state components
                    if 'llm' in locals() and llm is not None: # For OpenAI/Anthropic models
                        del llm
                    
                    # Use our complete unloading function
                    unload_model_completely(st.session_state)
                    force_cuda_release()
                    st.info("Model and memory cleanup complete.")
                   

if __name__ == "__main__":
    main()
