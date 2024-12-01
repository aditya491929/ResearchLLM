import os
import traceback
import numpy as np
import random
from flask import Flask, request, jsonify
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline, AutoModelForSeq2SeqLM
from pinecone import Pinecone
from langchain.text_splitter import RecursiveCharacterTextSplitter
import torch

# Ensure fallback for unsupported operations on MPS
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

# Pinecone credentials
api_key = ""
index_name = "research-paper-index"

pc = Pinecone(api_key=api_key)
index = pc.Index(index_name)

# Initialize Flask app
app = Flask(__name__)

# Load SentenceTransformer for embeddings
embedding_model = SentenceTransformer("all-MiniLM-L6-v2")

# Load LLaMA model
hf_token = ""
llama_model_name = "meta-llama/Llama-2-7b-chat-hf"

device = "cuda" if torch.cuda.is_available() else "cpu"

llama_tokenizer = AutoTokenizer.from_pretrained(llama_model_name, use_auth_token=hf_token)
llama_model = AutoModelForCausalLM.from_pretrained(
    llama_model_name,
    device_map="auto" if torch.cuda.is_available() else None,
    torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
    use_auth_token=hf_token
)

# Add padding token if necessary
if llama_tokenizer.pad_token is None:
    llama_tokenizer.add_special_tokens({'pad_token': '[PAD]'})
    llama_model.resize_token_embeddings(len(llama_tokenizer))

# Query Pinecone index
def query_pinecone(query, top_k=5):
    query_embedding = embedding_model.encode(query).tolist()
    results = index.query(vector=query_embedding, top_k=top_k, include_metadata=True)
    return results

# Generate answer using LLaMA
def generate_answer(query, matches):
    context = " ".join(
        [match.get("metadata", {}).get("chunk", "") for match in matches if "metadata" in match]
    )
    input_text = f"Query: {query}\nContext: {context}\nAnswer:"
    
    inputs = llama_tokenizer(
        input_text,
        return_tensors="pt",
        padding=True,
        truncation=True
    ).to(device)
    
    outputs = llama_model.generate(
        inputs.input_ids,
        max_length=300,
        pad_token_id=llama_tokenizer.pad_token_id
    )
    return llama_tokenizer.decode(outputs[0], skip_special_tokens=True)

@app.route('/')
def home():
    return "Welcome to the AI Query API! Use the `/query` endpoint to interact."

@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.route('/query', methods=['POST'])
def query():
    try:
        data = request.json
        query_text = data['query']
        
        pinecone_results = query_pinecone(query_text)
        matches = pinecone_results.get("matches", [])
        
        if not matches:
            return jsonify({"answer": "No relevant matches found in the database."})
        
        answer = generate_answer(query_text, matches)
        return jsonify({"answer": answer})
    except Exception as e:
        error_trace = traceback.format_exc()
        print(f"Error: {error_trace}")
        return jsonify({"error": str(e), "trace": error_trace}), 500

# Load the summarizer model
def load_summarizer(model_name="t5-small"):
    """
    Load the summarization model pipeline.
    Args:
        model_name (str): The name of the Hugging Face model.
    Returns:
        summarizer function
    """
    if model_name.startswith("t5"):
        # Use T5 summarizer
        model = AutoModelForSeq2SeqLM.from_pretrained(model_name)
        tokenizer = AutoTokenizer.from_pretrained(model_name)

        def t5_summarizer(text):
            input_ids = tokenizer.encode(f"summarize: {text}", return_tensors="pt", truncation=True, max_length=512)
            outputs = model.generate(input_ids, max_length=130, min_length=30, length_penalty=2.0, num_beams=4)
            return tokenizer.decode(outputs[0], skip_special_tokens=True)
        
        return t5_summarizer
    else:
        return None
    
# Split text into manageable chunks
def split_text_with_langchain(text, chunk_size=4096, chunk_overlap=200):
    """
    Splits the text into manageable chunks using LangChain's RecursiveCharacterTextSplitter.
    Args:
        text (str): The text to split.
        chunk_size (int): Maximum size of each chunk in tokens.
        chunk_overlap (int): Number of overlapping characters between chunks.
    Returns:
        List of text chunks.
    """
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", " ", ""]
    )
    chunks = text_splitter.split_text(text)
    return chunks

@app.route('/summarize', methods=['POST'])
def summarize():
    """
    Summarizes the uploaded text file.
    Expects a file to be uploaded as a POST request.
    """
    try:
        # Check if a file is uploaded
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        file = request.files['file']
        text = file.read().decode('utf-8')

        # Load summarization model
        model_name = "t5-small"  # Default model
        summarizer = load_summarizer(model_name)

        if summarizer is None:
            return jsonify({"error": "Model not supported"}), 500

        # Split text into chunks
        chunks = split_text_with_langchain(text, chunk_size=4096, chunk_overlap=200)

        # Summarize each chunk
        summaries = []
        for chunk in chunks:
            try:
                summary = summarizer(chunk)
                summaries.append(summary)
            except Exception as e:
                return jsonify({"error": f"Error summarizing chunk: {e}"}), 500

        # Combine all summaries
        final_summary = " ".join(summaries)
        return jsonify({"summary": final_summary})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)