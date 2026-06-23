
from cagent.metrics import run_real_context_experiment
result = run_real_context_experiment(provider='gpt', repetitions=1)
print('avg_full:', result['summary']['avg_full_prompt_chars'])
print('avg_raw:',  result['summary']['avg_raw_prompt_chars'])
print('avg_ratio:', result['summary']['avg_prompt_compression_ratio'])
print('max_ratio:', result['summary']['max_prompt_compression_ratio'])