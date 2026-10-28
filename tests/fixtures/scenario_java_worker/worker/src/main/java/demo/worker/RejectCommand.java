package demo.worker;

public class RejectCommand {
    private final Ledger ledger;
    private final String documentId;
    private final String reason;

    public RejectCommand(Ledger ledger, String documentId, String reason) {
        this.ledger = ledger;
        this.documentId = documentId;
        this.reason = reason;
    }

    public void execute() throws Exception {
        ledger.reject(documentId, reason);
    }
}
