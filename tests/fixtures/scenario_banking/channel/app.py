"""Synthetic example; no connection to real bank systems or customer data."""
from contracts import app, http


@app.post('/cards/debit/new')
def submit(request):
    """Educational input: a new debit card for an existing customer."""
    if request['client_type'] != 'existing':
        rejection = {'request_id': request['request_id'], 'status': 'unsupported_variant'}
        return rejection
    application = {
        'request_id': request['request_id'],
        'client_id': request['client_id'],
        'product': 'debit',
    }
    return http.post('https://applications.example/applications/debit', json=application)


@app.post('/cards/debit/reissue')
def reissue(request):
    return http.post('https://applications.example/applications/reissue', json=request)
