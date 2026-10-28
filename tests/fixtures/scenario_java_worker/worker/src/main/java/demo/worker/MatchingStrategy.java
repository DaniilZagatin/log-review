package demo.worker;

import demo.worker.handlers.ClarifyHandler;
import demo.worker.handlers.Handlers;

public class MatchingStrategy implements Strategy {
    private final Clients clients;
    private final Handlers handlers;
    private final ContractSearch search;

    public MatchingStrategy(Clients clients) {
        this.clients = clients;
        this.handlers = new Handlers(new TaskOperations(clients));
        this.search = new ContractSearch(clients.ledger(), handlers);
    }

    @Override
    public void process(Item item) throws Exception {
        Document document = clients.ledger().document(item.documentId());
        item.setDocument(document.id());
        if ("Agency".equals(document.kind())) {
            agencyPayment(item, document);
            return;
        }
        if (document.purpose().isEmpty() && document.payee().isEmpty() && document.account().isEmpty()) {
            handlers.forStatus(Status.CLARIFY).handle(item, document.id());
            return;
        }
        Details details = clients.extractor().extract(document.purpose() + " " + document.payee() + " " + document.account());
        if (!details.hasTwoOfThree()) {
            Status status = "Standard".equals(document.kind()) ? Status.STANDARD : Status.UNKNOWN;
            handlers.forStatus(status).handle(item, document.id());
            return;
        }
        search.settle(item, details);
    }

    private void agencyPayment(Item item, Document document) throws Exception {
        if (document.contract().isEmpty()) {
            handlers.forStatus(Status.CLARIFY).handle(item, document.id());
        } else {
            handlers.forStatus(Status.ACTIVE).handle(item, document.contract());
        }
    }
}
