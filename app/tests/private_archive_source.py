"""비공개 실측 사본 검증은 명시 환경변수로만 받으며 공통 합성 시험과 구분한다."""
from pathlib import Path
import os
import pytest

SOURCE_ENV='AICAF_PRIVATE_ARCHIVED_TEST_SOURCE'
SEMANTIC_ENV='AICAF_PRIVATE_SEMANTIC_ANALYSIS'
SOURCE=Path(os.environ.get(SOURCE_ENV) or '__private_archive_not_configured__')
SEMANTIC_SOURCE=Path(os.environ.get(SEMANTIC_ENV) or '__private_semantic_analysis_not_configured__')
PRIVATE_ARCHIVE_REQUIRED=pytest.mark.skipif(not os.environ.get(SOURCE_ENV),
    reason='private archived source must be explicitly configured; customer snapshots are not packaged in the product')
PRIVATE_SEMANTIC_REQUIRED=pytest.mark.skipif(not os.environ.get(SEMANTIC_ENV),
    reason='private semantic analysis must be explicitly configured; customer output is not a portable fixture')
