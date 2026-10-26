"""Synthetic issuing system; this creates educational records, not bank cards."""
from contracts import subscriber, issuer

cards = {}
results = {}


@subscriber('card.issue.requested')
def issue(message):
    """Educational consumer of the request_id, client_id and product contract."""
    parameters = {'request_id': message['request_id'], 'client_id': message['client_id'], 'product': message['product']}
    card = {
        'card_id': 'demo-' + parameters['request_id'],
        'client_id': parameters['client_id'],
        'product': parameters['product'],
        'status': 'issued',
    }
    cards[card['card_id']] = card
    results[parameters['request_id']] = {'status': 'issued', 'card_id': card['card_id']}


@subscriber('card.reissue.requested')
def reissue(message):
    issuer.replace_card(message['old_card_id'])
