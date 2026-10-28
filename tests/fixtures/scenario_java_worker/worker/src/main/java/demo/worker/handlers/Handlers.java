package demo.worker.handlers;

import demo.worker.Item;
import demo.worker.Status;
import demo.worker.TaskOperations;

public class Handlers {
    private final TaskOperations operations;

    public Handlers(TaskOperations operations) {
        this.operations = operations;
    }

    public Handler forStatus(Status status) {
        return switch (status) {
            case ACTIVE -> (item, id) -> operations.resubmitAndClose(item, id);
            case CLOSED -> (item, id) -> operations.rejectAndClose(item, id, "Contract already closed");
            case CLARIFY, STANDARD -> (item, id) -> operations.rejectAndClose(item, id, "Clarify payment details");
            default -> (item, id) -> operations.closeAndCreate(item);
        };
    }
}
