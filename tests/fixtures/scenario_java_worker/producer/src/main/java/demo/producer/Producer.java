package demo.producer;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;

// Synthetic fixture: a separate component that fills the worker's queue.
// It runs on its own schedule and is not a step of one payment's route.
public class Producer {
    private final TaskBoard board;
    private final TaskQueue queue;

    public Producer(TaskBoard board, TaskQueue queue) {
        this.board = board;
        this.queue = queue;
    }

    public void fillQueue() throws Exception {
        List<Item> items = new ArrayList<>();
        LocalDateTime startTime = LocalDateTime.now();
        while (startTime.plusMinutes(5).isAfter(LocalDateTime.now()) && !queue.isStopRequested()) {
            Task task = board.nextTask();
            if (task == null || task.id().isEmpty()) {
                break;
            }
            items.add(Item.from(board.taskInfo(task.id())));
            board.postpone(task.id());
        }
        for (Item item : items) {
            queue.add(item.number(), item);
        }
    }
}
