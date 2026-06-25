"""Shared constants for scraping + scoring."""

SITES = ["linkedin", "indeed", "google", "glassdoor"]
COUNTRY_INDEED = "USA"

RESULTS_WANTED_PER_TERM = 25
DEFAULT_HOURS_OLD = 24
FALLBACK_HOURS = [72, 168, 336]
RESUME_ANALYSIS_VERSION = 2
PREFILTER_BYPASS_LIMIT = 25
JOB_CACHE_MAX_AGE_MINUTES = 60
JOB_CACHE_REFRESH_HOURS = 168
MIN_RAW_JOBS_FOR_RESULTS = 20
SEEN_TTL_HOURS = 24

DEFAULT_SEARCH_TITLES = [
    "AI Engineer",
    "Machine Learning Engineer",
    "GenAI Engineer",
    "LLM Engineer",
    "Applied AI Engineer",
    "Applied Scientist",
    "Data Scientist",
    "ML Engineer",
    "ML Platform Engineer",
]

DEFAULT_STEM_SEARCH_TITLES = [
    "AI Engineer",
    "Machine Learning Engineer",
    "ML Engineer",
    "GenAI Engineer",
    "LLM Engineer",
    "Applied AI Engineer",
    "Applied Scientist",
    "Data Scientist",
    "Data Engineer",
    "Analytics Engineer",
    "Software Engineer",
    "Backend Engineer",
    "Platform Engineer",
    "MLOps Engineer",
]

DEFAULT_SKILL_SIGNALS = [
    "Python",
    "SQL",
    "Machine Learning",
    "LLM",
    "RAG",
    "Snowflake",
    "dbt",
]

RESUME_SKILL_KEYWORDS = [
    "Python",
    "SQL",
    "Machine Learning",
    "Deep Learning",
    "LLM",
    "RAG",
    "LangChain",
    "LangGraph",
    "OpenAI",
    "Anthropic",
    "MCP",
    "semantic search",
    "semantic reranking",
    "vector database",
    "vector databases",
    "embeddings",
    "Snowflake",
    "dbt",
    "Airflow",
    "Spark",
    "AWS",
    "GCP",
    "Azure",
    "Docker",
    "Kubernetes",
    "PyTorch",
    "TensorFlow",
]

WEIGHTS = {"keyword": 0.45, "resume": 0.25, "recency": 0.30}
MIN_KEYWORD_SCORE = 0.10
TOP_RESULTS = 10
SCRAPE_TIMEOUT_SECONDS = 120
CLAUDE_TIMEOUT_SECONDS = 30

AI_KEYWORDS = {
    "machine learning": 3, "deep learning": 3, "ai engineer": 4,
    "ml engineer": 4, "applied scientist": 3, "llm": 3, "large language model": 3,
    "generative ai": 3, "genai": 3, "mlops": 3, "neural network": 2,
    "pytorch": 2, "tensorflow": 2, "hugging face": 2, "huggingface": 2,
    "langchain": 2, "transformers": 2, "scikit-learn": 1, "keras": 1,
    "vector database": 2, "rag": 2, "retrieval augmented": 2, "fine-tuning": 2,
    "fine tuning": 2, "embeddings": 2, "diffusion": 2, "computer vision": 2,
    "nlp": 2, "natural language processing": 2, "reinforcement learning": 2,
    "kubernetes": 1, "docker": 1, "aws": 1, "gcp": 1, "azure": 1,
    "spark": 1, "airflow": 1, "model serving": 2, "inference": 2,
    "feature store": 1, "data pipeline": 1, "python": 1,
}
KW_NORM = sum(sorted(AI_KEYWORDS.values(), reverse=True)[:8])

TITLE_BLOCKLIST = [
    "recruit", "sales", "account executive", "intern,", "internship",
    "professor", "lecturer", "teacher", "marketing", "customer success",
]

STOPWORDS = set("""a an the and or of to in for with on at by from as is are be this that
your you we our will have has can able role team work years experience strong
skills ability requirements preferred plus etc using use used job position company""".split())

# How long a finished/errored job stays in memory before being pruned.
JOB_TTL_SECONDS = 60 * 60  # 1 hour
