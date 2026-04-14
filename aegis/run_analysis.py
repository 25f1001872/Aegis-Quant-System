import sys
import os
import pandas as pd
from glob import glob
import yaml

# Add the project's root directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Import from the new standardized signal file
from aegis.signals.s2_oi_delta import S2OIDeltaSignal

def load_config():
    """Load configuration from YAML file"""
    config_path = 'configs/oi_delat_config.yaml'
    if not os.path.exists(config_path):
        print(f"⚠️ Config not found at {config_path}, using defaults.")
        return {}
    
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def run_oi_delta_analysis():
    print("🚀 Starting AEGIS OI Delta Batch Analysis...")
    
    # Load parameters from config
    full_config = load_config()
    params = full_config.get('parameters', {})
    
    print(f"📊 Using Parameters: Window={params.get('zscore_window', 30)}, Threshold={params.get('threshold_up', 0.5)}")
    
    # Define paths
    dataset_dir = 'aegis/dataset'
    results_dir = os.path.join(dataset_dir, 'results')
    
    # Ensure results directory exists
    if not os.path.exists(results_dir):
        os.makedirs(results_dir)
        print(f"Created results directory: {results_dir}")

    # Find all CSV files in the dataset folder
    csv_files = glob(os.path.join(dataset_dir, "*.csv"))
    
    if not csv_files:
        print("❌ No CSV files found in aegis/dataset/")
        return

    # Initialize Signal with config parameters
    signal = S2OIDeltaSignal(
        threshold_up=params.get('threshold_up', 0.5),
        threshold_down=params.get('threshold_down', -0.5),
        use_zscore=params.get('use_zscore', True),
        zscore_window=params.get('zscore_window', 30)
    )
    
    for file_path in csv_files:
        file_name = os.path.basename(file_path)
        print(f"\nProcessing: {file_name}...")
        
        try:
            # Load data
            df = pd.read_csv(file_path)
            
            # --- Handle different column naming conventions ---
            
            # Case 1: Snake case (open_interest.csv)
            if 'sum_open_interest' in df.columns and 'sum_open_interest_value' in df.columns:
                df['close'] = df['sum_open_interest_value'] / df['sum_open_interest']
                df.rename(columns={'sum_open_interest': 'oi_btc'}, inplace=True)
                
            # Case 2: Camel case (btc_open_interest.csv)
            elif 'sumOpenInterest' in df.columns and 'sumOpenInterestValue' in df.columns:
                df['close'] = df['sumOpenInterestValue'] / df['sumOpenInterest']
                df.rename(columns={'sumOpenInterest': 'oi_btc'}, inplace=True)
                
            # Fallback check
            if 'oi_btc' not in df.columns or 'close' not in df.columns:
                print(f"  ⚠️ Skipping {file_name}: Could not map required columns (oi_btc, close).")
                print(f"  Available columns: {list(df.columns)}")
                continue

            # Calculate signals
            df_result = signal.calculate(df)
            
            # Save results
            output_path = os.path.join(results_dir, f"result_{file_name}")
            df_result.to_csv(output_path, index=False)
            print(f"  ✅ Saved to: {output_path}")
            
            # Print stats
            stats = signal.get_signal_stats(df_result)
            print(f"  📊 Stats: Bullish {stats['bullish_ratio']}% | Bearish {stats['bearish_ratio']}% | Neutral {stats['neutral_ratio']}%")
            
        except Exception as e:
            print(f"  ❌ Error processing {file_name}: {str(e)}")

    print("\n✅ Batch analysis complete. Results are in 'aegis/dataset/results/'")

if __name__ == "__main__":
    run_oi_delta_analysis()
