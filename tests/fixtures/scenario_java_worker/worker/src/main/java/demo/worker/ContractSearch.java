package demo.worker;

import demo.worker.handlers.Handlers;
import java.util.ArrayList;
import java.util.List;

public class ContractSearch {
    private final Ledger ledger;
    private final Handlers handlers;

    public ContractSearch(Ledger ledger, Handlers handlers) {
        this.ledger = ledger;
        this.handlers = handlers;
    }

    public void settle(Item item, Details details) throws Exception {
        List<Contract> rows = findRows(details);
        if (rows.isEmpty()) {
            Status status = "Standard".equals(item.paymentKind()) ? Status.CLARIFY : Status.UNKNOWN;
            handlers.forStatus(status).handle(item, item.documentId());
            return;
        }
        if (rows.size() > 3) {
            handlers.forStatus(Status.UNKNOWN).handle(item, null);
            return;
        }
        Contract contract = rows.get(0);
        switch (contract.state()) {
            case "Active" -> handlers.forStatus(Status.ACTIVE).handle(item, contract.id());
            case "Closed" -> handlers.forStatus(Status.CLOSED).handle(item, contract.id());
            default -> handlers.forStatus(Status.UNKNOWN).handle(item, contract.id());
        }
    }

    private List<Contract> findRows(Details details) throws Exception {
        if (details.account() != null && details.contractNumber() != null) {
            List<Contract> result = ledger.find(details.account(), details.contractNumber(), null);
            if (result.isEmpty() && details.hasNames()) {
                result = ledger.find(null, details.contractNumber(), details.names());
                if (result.isEmpty()) {
                    result = ledger.find(details.account(), null, details.names());
                }
            }
            return result;
        }
        if (details.hasNames()) {
            return ledger.find(details.account(), details.contractNumber(), details.names());
        }
        return new ArrayList<>();
    }
}
