"""Synthetic request processing; dictionaries are educational in-memory stores."""
from contracts import app, broker

client_profiles = {'client-demo': {'active': True, 'debit_allowed': True}}
applications = {}


@app.post('/applications/debit')
def accept(request):
    """Educational route receiving request_id, client_id and product."""
    profile = client_profiles[request['client_id']]
    if not (profile['active'] and profile['debit_allowed']):
        applications[request['request_id']] = {'status': 'rejected', 'client_id': request['client_id']}
        rejection = {'request_id': request['request_id'], 'status': 'rejected'}
        return rejection
    applications[request['request_id']] = {'status': 'accepted', 'client_id': request['client_id']}
    message = {
        'request_id': request['request_id'],
        'client_id': request['client_id'],
        'product': request['product'],
    }
    broker.publish('card.issue.requested', message)


def retry_after_timeout(request_id):
    broker.publish('card.issue.requested', {'request_id': request_id})
