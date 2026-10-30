# Synthetic service request process

This is an original artificial example. It contains no real application,
customer, employee or production telemetry. All identifiers and dates are invented.

One case is one service request. A request is received, reviewed and fulfilled.
A review may return the same request for revision; the next review is a new
business operation occurrence within that case. Fulfilment may have a transient
technical failure and retry within the same operation occurrence. A repeated
delivery of a record is neither a new attempt nor a new business occurrence.

The required cancellation alternative remains part of the process even though
the three controlled scenarios do not take it. The illustrative application has
an uninstrumented cancellation function. This absence alone would not establish
zero opportunities in a real application; here the scenario table independently
defines the complete controlled input population.

`app.py` is analysis input only. Never import or run it. `generate.py` is trusted
test infrastructure: it generates records from `scenarios.json` without reading
or executing the application. `ground_truth.json` is a test oracle, never an
input to the agent or a scripted semantic model. No semantic model exists yet.

The event clock is UTC. Each original record is delivered two seconds after its
event time; a duplicated delivery arrives fifteen seconds after the same event.
The controlled population has three completed cases, ten business operation
occurrences, eleven technical attempts, nineteen logical events and twenty
delivered records. Case durations are 300, 1200 and 480 seconds respectively.
Duration is measured from the controlled `started_at` time to the
`fulfil.completed` event time, never its delivery time. Business activity labels
are absent from raw logs; only the test oracle contains the reference mapping.
