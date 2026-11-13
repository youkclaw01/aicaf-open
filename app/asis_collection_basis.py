"""수집 스크립트의 기초 JSON·Excel을 바이트 SHA·원천 바인딩으로 검증한다."""
from copy import deepcopy
from io import BytesIO
import base64,hashlib,zipfile
import openpyxl


def workbook_bytes(descriptor):
    if not isinstance(descriptor,dict) or descriptor.get('schema')!='ai-assessment-basis-workbook/v1':
        raise ValueError('collection_basis_workbook_schema')
    encoded=descriptor.get('base64')
    if not isinstance(encoded,str) or not encoded or len(encoded)>32*1024*1024:
        raise ValueError('collection_basis_workbook_content')
    try:raw=base64.b64decode(encoded,validate=True)
    except (ValueError,TypeError) as exc:raise ValueError('collection_basis_workbook_content') from exc
    if hashlib.sha256(raw).hexdigest()!=descriptor.get('sha256'):
        raise ValueError('collection_basis_workbook_hash')
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            names=archive.namelist()
            if sum(i.file_size for i in archive.infolist())>128*1024*1024:
                raise ValueError('collection_basis_workbook_expansion')
            if any('vbaproject' in n.casefold() or 'externallinks/' in n.casefold() or n.startswith('/') or '..' in n.split('/') for n in names):
                raise ValueError('collection_basis_workbook_unsafe_package')
        book=openpyxl.load_workbook(BytesIO(raw),read_only=True,data_only=False)
        book.close()
    except (zipfile.BadZipFile,KeyError) as exc:raise ValueError('collection_basis_workbook_package') from exc
    return raw


def extract(evidence,topology_bytes):
    if not any(key in evidence for key in ('assessmentBasis','assessmentBasisWorkbook','assessmentBasisArtifact')):
        return {}
    basis=evidence.get('assessmentBasis');descriptor=evidence.get('assessmentBasisWorkbook');artifact=evidence.get('assessmentBasisArtifact')
    if not isinstance(basis,dict) or basis.get('schema')!='ai-assessment-basis/v1':
        raise ValueError('collection_basis_schema')
    original=basis.get('source_artifacts',{}).get('topology.json',{})
    if original.get('sha256')!=hashlib.sha256(topology_bytes).hexdigest():
        raise ValueError('collection_basis_source_binding')
    workbook_bytes(descriptor)
    if not isinstance(artifact,dict) or artifact.get('workbook_sha256')!=descriptor.get('sha256'):
        raise ValueError('collection_basis_workbook_artifact_binding')
    return {'collection_basis':deepcopy(basis),'collection_basis_workbook':deepcopy(descriptor),
            'collection_basis_verified':True,'collection_basis_assessment_status':'not_evaluated'}
