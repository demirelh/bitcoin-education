# Constraints Table

This table lists the constraints and limitations that prevent full certainty
about model access and availability.

| Constraint Category | Explanation |
|---------------------|-------------|
| Dependency on System Context | My knowledge of the current runtime model depends entirely on the system providing accurate context information in my prompt. If this information is incorrect or omitted, I cannot independently verify which model I am. |
| No Direct API Access | I cannot directly query the model serving infrastructure or make API calls to verify which models are truly available at runtime. I can only infer from configuration files, code, and environment variables. |
| Configuration vs Runtime Reality | Configuration files (.env.example) show intended models, but actual runtime configuration may differ. The real .env file may specify different models, API keys may be invalid, or the deployment may use different settings than what's documented. |
| Code Inspection Limitations | While I can read the codebase and see that it imports 'anthropic' and 'openai' packages, I cannot verify: (1) if these dependencies are actually installed, (2) if API keys are valid, (3) if network access permits reaching these services, or (4) what models are available through these APIs at runtime. |
| Model Routing Internals | The Task tool indicates model routing capability (haiku/sonnet/opus), but I don't have visibility into the internal routing logic, fallback mechanisms, or whether all specified models are actually available. |
| Temporal Limitations | My knowledge cutoff is January 2025. Newer models released after this date would not be in my training data, though I may learn about them through system context or documentation in the codebase. |

## Summary

These constraints highlight the inherent limitations in determining exact model 
availability and capabilities at runtime. The system relies on configuration files, 
code inspection, and system context, but cannot independently verify runtime 
infrastructure details.
