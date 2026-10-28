package demo.worker;

public class ResubmitCommand {
    private final Ledger ledger;
    private final String documentId;
    private final String contractId;

    public ResubmitCommand(Ledger ledger, String documentId, String contractId) {
        this.ledger = ledger;
        this.documentId = documentId;
        this.contractId = contractId;
    }

    public void execute() throws Exception {
        ledger.resubmit(documentId, contractId);
    }
}
