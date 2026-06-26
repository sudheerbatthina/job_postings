"""Curated company job-source registry.

The registry is intentionally simple data so adding/correcting a company source
does not require changing ingestion code.
"""

COMPANIES = [
    {"company": "OpenAI", "ats": "greenhouse", "slug": "openai", "career_url": "https://openai.com/careers"},
    {"company": "Anthropic", "ats": "greenhouse", "slug": "anthropic", "career_url": "https://www.anthropic.com/jobs"},
    {"company": "Databricks", "ats": "greenhouse", "slug": "databricks", "career_url": "https://www.databricks.com/company/careers"},
    {"company": "Snowflake", "ats": "greenhouse", "slug": "snowflake", "career_url": "https://careers.snowflake.com"},
    {"company": "NVIDIA", "ats": "workday", "slug": "nvidia", "career_url": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"},
    {"company": "Google", "ats": "custom", "slug": "google", "career_url": "https://www.google.com/about/careers/applications/jobs/results"},
    {"company": "Microsoft", "ats": "custom", "slug": "microsoft", "career_url": "https://jobs.careers.microsoft.com"},
    {"company": "Meta", "ats": "custom", "slug": "meta", "career_url": "https://www.metacareers.com/jobs"},
    {"company": "Amazon", "ats": "custom", "slug": "amazon", "career_url": "https://www.amazon.jobs"},
    {"company": "Apple", "ats": "custom", "slug": "apple", "career_url": "https://jobs.apple.com"},
    {"company": "Tesla", "ats": "custom", "slug": "tesla", "career_url": "https://www.tesla.com/careers"},
    {"company": "Netflix", "ats": "lever", "slug": "netflix", "career_url": "https://jobs.netflix.com"},
    {"company": "Stripe", "ats": "greenhouse", "slug": "stripe", "career_url": "https://stripe.com/jobs"},
    {"company": "Coinbase", "ats": "greenhouse", "slug": "coinbase", "career_url": "https://www.coinbase.com/careers"},
    {"company": "Scale AI", "ats": "greenhouse", "slug": "scaleai", "career_url": "https://scale.com/careers"},
    {"company": "Cohere", "ats": "greenhouse", "slug": "cohere", "career_url": "https://cohere.com/careers"},
    {"company": "Hugging Face", "ats": "greenhouse", "slug": "huggingface", "career_url": "https://huggingface.co/jobs"},
    {"company": "Perplexity", "ats": "ashby", "slug": "perplexity", "career_url": "https://www.perplexity.ai/hub/careers"},
    {"company": "Together AI", "ats": "ashby", "slug": "togetherai", "career_url": "https://www.together.ai/careers"},
    {"company": "LangChain", "ats": "ashby", "slug": "langchain", "career_url": "https://www.langchain.com/careers"},
    {"company": "Pinecone", "ats": "greenhouse", "slug": "pinecone", "career_url": "https://www.pinecone.io/careers"},
    {"company": "Weaviate", "ats": "lever", "slug": "weaviate", "career_url": "https://weaviate.io/careers"},
    {"company": "MongoDB", "ats": "greenhouse", "slug": "mongodb", "career_url": "https://www.mongodb.com/careers"},
    {"company": "Palantir", "ats": "lever", "slug": "palantir", "career_url": "https://www.palantir.com/careers"},
    {"company": "Anduril", "ats": "greenhouse", "slug": "andurilindustries", "career_url": "https://www.anduril.com/careers"},
    {"company": "ServiceNow", "ats": "workday", "slug": "servicenow", "career_url": "https://careers.servicenow.com"},
    {"company": "Salesforce", "ats": "workday", "slug": "salesforce", "career_url": "https://careers.salesforce.com"},
    {"company": "Adobe", "ats": "workday", "slug": "adobe", "career_url": "https://careers.adobe.com"},
    {"company": "Oracle", "ats": "custom", "slug": "oracle", "career_url": "https://www.oracle.com/careers"},
    {"company": "Uber", "ats": "greenhouse", "slug": "uber", "career_url": "https://www.uber.com/careers"},
    {"company": "Airbnb", "ats": "greenhouse", "slug": "airbnb", "career_url": "https://careers.airbnb.com"},
    {"company": "DoorDash", "ats": "greenhouse", "slug": "doordash", "career_url": "https://careers.doordash.com"},
    {"company": "Instacart", "ats": "greenhouse", "slug": "instacart", "career_url": "https://www.instacart.com/company/careers"},
]
