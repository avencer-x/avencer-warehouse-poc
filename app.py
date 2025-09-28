import streamlit as st
import pandas as pd
import json
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv
from PIL import Image
import io
import traceback

# --- SETUP ---
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    st.error("CRITICAL ERROR: GOOGLE_API_KEY not found in environment variables.")
    st.stop()

# Initialize the GenAI client
client = genai.Client(api_key=GOOGLE_API_KEY)

# --- SCHEMAS ---
CHALLAN_JSON_SCHEMA = """
{
"challan_number": "string | null",
"date": "YYYY-MM-DD | null",
"lines": [
    {
    "sto_sku": "string | null",
    "material_description": "string",
    "hsn": "string | null",
    "size": "string",
    "qty_units_expected": "integer"
    }
]
}
"""

STICKER_JSON_SCHEMA = """
{
"style": "string",
"code_size": "string",
"mrp": "number | null",
"net_qty": "integer | null"
}
"""

# --- STREAMLIT CONFIG ---
st.set_page_config(
    page_title="Warehouse Reconciliation POC",
    page_icon="🚚",
    layout="wide"
)

# --- SESSION STATE INITIALIZATION ---
if 'challan_data' not in st.session_state:
    st.session_state.challan_data = {}
if 'scanned_stickers' not in st.session_state:
    st.session_state.scanned_stickers = []

# --- CORE FUNCTIONS ---
def process_image_with_ai(image_file, document_type):
    """Process image using Google Gemini AI"""
    try:
        schema = CHALLAN_JSON_SCHEMA if document_type == "CHALLAN" else STICKER_JSON_SCHEMA
        
        text_prompt = f"""
        Analyze the provided image of a '{document_type}'.
        Your task is to extract the key information and return ONLY a single, strictly valid JSON object that conforms precisely to the schema below.
        Do not add any explanatory text, comments, or markdown formatting like ```json. Your entire response must be the JSON object itself.

        Key Instructions:
        - For CHALLANS, the 'sto_sku' is the numeric code in the 'STO' column. The 'material_description' is the text in the 'Material Description' column. You MUST extract them as separate fields. Do not merge them.
        - For CHALLANS with a grid of sizes, create a separate line item in the JSON for each size and its quantity. So basically the Size of the product should be taken from here
        - For STICKERS, 'code_size' should be the most prominent size indicator and would be mentioned in the with "Code:" (e.g., 'S', '36B').
        - The quantity from the challan is always the quantity of the boxes (not the individual units inside them)

        Schema to follow:
        {schema}
        """

        # Convert uploaded file to bytes
        image_bytes = image_file.getvalue()
        
        # Create image part using the new API
        image_part = types.Part.from_bytes(
            data=image_bytes,
            mime_type=image_file.type or 'image/jpeg'
        )

        # Use the new client with thinking disabled
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[text_prompt, image_part],
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0)
            )
        )
        
        raw_text = response.text.strip().replace("```json", "").replace("```", "").strip()
        json_data = json.loads(raw_text)
        
        return json_data, None
        
    except json.JSONDecodeError as e:
        return None, f"Failed to decode JSON from AI model. Raw response: {raw_text}"
    except Exception as e:
        return None, f"An error occurred: {str(e)}"

def clear_session():
    """Clear all session data"""
    st.session_state.challan_data = {}
    st.session_state.scanned_stickers = []
    st.success("Session cleared! Please upload a new challan.")

def run_reconciliation():
    """Run reconciliation between challan and scanned stickers"""
    if not st.session_state.challan_data or 'lines' not in st.session_state.challan_data:
        st.warning("No challan data to reconcile. Please upload a challan first.")
        return None
    
    expected_items = {}
    for line in st.session_state.challan_data.get('lines', []):
        key = (
            str(line.get('material_description', '')).strip(),
            str(line.get('size', '')).strip().upper()
        )
        if key not in expected_items:
            expected_items[key] = {'expected': 0, 'received': 0}
        expected_items[key]['expected'] += line.get('qty_units_expected', 0)
    
    unmatched_stickers = []

    for sticker in st.session_state.scanned_stickers:
        sticker_style = str(sticker.get('style', '')).strip()
        sticker_size = str(sticker.get('code_size', '')).strip().upper()
        
        match_found = False
        for challan_key in expected_items.keys():
            challan_desc = challan_key[0]
            challan_size = challan_key[1]
            
            if challan_desc.startswith(sticker_style) and challan_size == sticker_size:
                expected_items[challan_key]['received'] += 1
                match_found = True
                break
        
        if not match_found:
            unmatched_stickers.append(sticker)

    report_data = []
    for key, data in expected_items.items():
        variance = data['received'] - data['expected']
        status = "✅ MATCH" if variance == 0 else ("⚠️ SHORT" if variance < 0 else "❗️ OVER")
        report_data.append({
            "Challan Description": key[0],
            "Size": key[1],
            "Expected": data['expected'],
            "Received": data['received'],
            "Variance": variance,
            "Status": status
        })
    
    for sticker in unmatched_stickers:
        report_data.append({
            "Challan Description": f"(UNMATCHED SCAN) {sticker.get('style','')}",
            "Size": sticker.get('code_size',''),
            "Expected": 0,
            "Received": 1,
            "Variance": 1,
            "Status": "❗️ OVER"
        })

    return pd.DataFrame(report_data)

# --- MAIN UI ---
st.title("🚚 Warehouse Inbound Reconciliation POC")

# Sidebar for session management
with st.sidebar:
    st.header("Session Management")
    if st.button("🔄 Clear Session / Start New", type="secondary"):
        clear_session()
    
    st.header("Current Status")
    if st.session_state.challan_data:
        st.success(f"✅ Challan loaded: {st.session_state.challan_data.get('challan_number', 'N/A')}")
    else:
        st.info("📋 No challan loaded")
    
    st.info(f"📦 Stickers scanned: {len(st.session_state.scanned_stickers)}")

# Main tabs
tab1, tab2 = st.tabs(["📥 Inbound Processing", "📊 Reconciliation Dashboard"])

with tab1:
    st.header("Step 1: Upload Delivery Challan")
    
    challan_file = st.file_uploader(
        "Upload Delivery Challan Image",
        type=['png', 'jpg', 'jpeg'],
        key="challan_uploader"
    )
    
    if challan_file is not None:
        col1, col2 = st.columns([1, 2])
        
        with col1:
            st.image(challan_file, caption="Uploaded Challan", use_column_width=True)
        
        with col2:
            if st.button("🔍 Process Challan", type="primary"):
                with st.spinner("Processing challan with AI..."):
                    result, error = process_image_with_ai(challan_file, "CHALLAN")
                    
                    if error:
                        st.error(f"Error processing challan: {error}")
                    else:
                        st.session_state.challan_data = result
                        st.success("Challan processed successfully!")
                        st.rerun()
    
    # Display challan info if available
    if st.session_state.challan_data:
        st.subheader("📋 Extracted Challan Details")
        
        col1, col2 = st.columns(2)
        with col1:
            st.metric("Challan Number", st.session_state.challan_data.get('challan_number', 'N/A'))
        with col2:
            st.metric("Date", st.session_state.challan_data.get('date', 'N/A'))
        
        if 'lines' in st.session_state.challan_data:
            st.subheader("📦 Challan Line Items")
            df = pd.DataFrame(st.session_state.challan_data['lines'])
            st.dataframe(df, use_container_width=True)
    
    st.divider()
    
    # Sticker scanning section
    st.header("Step 2: Upload Product Stickers")
    
    if not st.session_state.challan_data:
        st.warning("⚠️ Please process a delivery challan first!")
    else:
        sticker_files = st.file_uploader(
            "Upload Product Sticker(s)",
            type=['png', 'jpg', 'jpeg'],
            accept_multiple_files=True,
            key="sticker_uploader"
        )
        
        if sticker_files:
            cols = st.columns(min(len(sticker_files), 4))
            
            for idx, sticker_file in enumerate(sticker_files):
                with cols[idx % 4]:
                    st.image(sticker_file, caption=f"Sticker {idx+1}", use_column_width=True)
            
            if st.button("🔍 Process All Stickers", type="primary"):
                progress_bar = st.progress(0)
                status_text = st.empty()
                
                for idx, sticker_file in enumerate(sticker_files):
                    status_text.text(f"Processing sticker {idx+1}/{len(sticker_files)}...")
                    progress_bar.progress((idx + 1) / len(sticker_files))
                    
                    result, error = process_image_with_ai(sticker_file, "STICKER")
                    
                    if error:
                        st.error(f"Error processing {sticker_file.name}: {error}")
                    else:
                        st.session_state.scanned_stickers.append(result)
                
                status_text.text("All stickers processed!")
                st.success(f"Successfully processed {len(sticker_files)} stickers!")
                st.rerun()
    
    # Display scanned stickers
    if st.session_state.scanned_stickers:
        st.subheader("📦 Scanned Stickers Log")
        stickers_df = pd.DataFrame(st.session_state.scanned_stickers)
        st.dataframe(stickers_df, use_container_width=True)

with tab2:
    st.header("📊 Reconciliation Dashboard")
    
    if not st.session_state.challan_data:
        st.warning("⚠️ No challan data available. Please upload and process a challan first.")
    elif not st.session_state.scanned_stickers:
        st.warning("⚠️ No stickers scanned yet. Please scan some stickers first.")
    else:
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric("Expected Items", len(st.session_state.challan_data.get('lines', [])))
        with col2:
            st.metric("Scanned Stickers", len(st.session_state.scanned_stickers))
        with col3:
            if st.button("🚀 Run Reconciliation", type="primary"):
                with st.spinner("Running reconciliation..."):
                    report_df = run_reconciliation()
                    if report_df is not None:
                        st.session_state.reconciliation_report = report_df
                        st.rerun()
        
        # Display reconciliation report
        if 'reconciliation_report' in st.session_state:
            st.subheader("📋 Reconciliation Report")
            
            # Summary metrics
            report_df = st.session_state.reconciliation_report
            matches = len(report_df[report_df['Status'] == '✅ MATCH'])
            shorts = len(report_df[report_df['Status'] == '⚠️ SHORT'])
            overs = len(report_df[report_df['Status'] == '❗️ OVER'])
            
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("✅ Matches", matches)
            with col2:
                st.metric("⚠️ Shortages", shorts)
            with col3:
                st.metric("❗️ Overages", overs)
            
            # Detailed report
            st.dataframe(
                report_df,
                use_container_width=True,
                column_config={
                    "Status": st.column_config.TextColumn(
                        "Status",
                        width="small"
                    ),
                    "Variance": st.column_config.NumberColumn(
                        "Variance",
                        format="%d"
                    )
                }
            )
            
            # Download report
            csv = report_df.to_csv(index=False)
            st.download_button(
                label="📥 Download Report as CSV",
                data=csv,
                file_name="reconciliation_report.csv",
                mime="text/csv"
            )

# Footer
st.divider()
st.markdown("*Warehouse Reconciliation POC - Powered by Google Gemini AI*")