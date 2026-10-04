import 'package:flutter/material.dart';

import '../api/models.dart';
import '../util/time_format.dart';

const metricLabels = {
  'heart_rate': 'Heart rate',
  'resting_heart_rate': 'Resting heart rate',
  'steps': 'Steps',
  'sleep': 'Sleep',
  'hrv_rmssd': 'HRV (RMSSD)',
};

/// Always-visible LIVE / SIMULATED badge. Simulation is never styled like live data.
class SourceBadge extends StatelessWidget {
  const SourceBadge({super.key, required this.simulation});
  final bool simulation;

  @override
  Widget build(BuildContext context) {
    final color = simulation ? Colors.deepPurple : Colors.teal.shade700;
    return Semantics(
      label: simulation ? 'Simulated data' : 'Live data',
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
        decoration: BoxDecoration(
          color: color,
          borderRadius: BorderRadius.circular(20),
        ),
        child: Text(
          simulation ? 'SIMULATED' : 'LIVE',
          style: const TextStyle(
            color: Colors.white,
            fontWeight: FontWeight.bold,
          ),
        ),
      ),
    );
  }
}

class SimulationBanner extends StatelessWidget {
  const SimulationBanner({super.key, required this.text});
  final String text;

  @override
  Widget build(BuildContext context) => Container(
    width: double.infinity,
    color: Colors.deepPurple,
    padding: const EdgeInsets.all(12),
    child: Text(
      text,
      key: const Key('simulation-banner'),
      textAlign: TextAlign.center,
      style: const TextStyle(
        color: Colors.white,
        fontSize: 18,
        fontWeight: FontWeight.bold,
      ),
    ),
  );
}

({String label, Color color, IconData icon}) stateStyle(String state) =>
    switch (state) {
      'fresh' => (
        label: 'Current',
        color: Colors.green.shade700,
        icon: Icons.check_circle,
      ),
      'stale' => (
        label: 'NOT CURRENT',
        color: Colors.orange.shade800,
        icon: Icons.history,
      ),
      'waiting_for_data' => (
        label: 'Waiting for data',
        color: Colors.blueGrey,
        icon: Icons.hourglass_empty,
      ),
      'permission_denied' => (
        label: 'Permission not granted',
        color: Colors.red.shade700,
        icon: Icons.block,
      ),
      _ => (
        label: 'Not available',
        color: Colors.grey,
        icon: Icons.remove_circle_outline,
      ),
    };

String formatValue(String metric, double v) => switch (metric) {
  'sleep' => '${v ~/ 60} h ${(v % 60).round()} min',
  'steps' => v.round().toString(),
  _ => '${v.round()}',
};

String unitLabel(String metric, String unit) => switch (metric) {
  'sleep' => '',
  'steps' => 'steps',
  _ => unit,
};

/// One metric with its freshness state. A stale value is shown greyed with its age and
/// explicitly labelled NOT CURRENT; it is never presented as the current reading.
class MetricCard extends StatelessWidget {
  const MetricCard({super.key, required this.status, required this.wearerTz});
  final MetricStatus status;
  final String wearerTz;

  @override
  Widget build(BuildContext context) {
    final st = stateStyle(status.state);
    final l = status.latest;
    final fresh = status.isFresh;
    final theme = Theme.of(context);
    return Card(
      key: Key('metric-${status.metric}'),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    metricLabels[status.metric] ?? status.metric,
                    style: theme.textTheme.titleLarge,
                  ),
                ),
                Icon(st.icon, color: st.color),
                const SizedBox(width: 6),
                Text(
                  st.label,
                  key: Key('state-${status.metric}'),
                  style: TextStyle(
                    color: st.color,
                    fontWeight: FontWeight.bold,
                    fontSize: 16,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 8),
            if (l != null)
              Text(
                '${formatValue(status.metric, l.value)} ${unitLabel(status.metric, l.unit)}',
                style: theme.textTheme.displaySmall?.copyWith(
                  color: fresh ? null : Colors.grey,
                  decoration: fresh ? null : TextDecoration.lineThrough,
                  decorationThickness: 1,
                ),
              ),
            if (l != null) ...[
              Text(
                'Measured ${formatAge(status.ageSeconds)} · ${formatDual(l.endAt ?? l.measuredAt, wearerTz)}',
                style: theme.textTheme.bodyLarge,
              ),
              Text(
                'Received ${formatDual(l.receivedAt, wearerTz)}',
                style: theme.textTheme.bodyMedium,
              ),
              Text(
                'Source: ${l.isSimulated ? 'SIMULATION' : l.dataOrigin}'
                '${l.deviceModel != null ? ' · ${l.deviceModel}' : ''}',
                style: theme.textTheme.bodyMedium,
              ),
            ],
            if (status.reason != null) ...[
              const SizedBox(height: 4),
              Text(
                status.reason!,
                style: theme.textTheme.bodyMedium?.copyWith(color: st.color),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class BigButton extends StatelessWidget {
  const BigButton({
    super.key,
    required this.label,
    required this.onPressed,
    this.icon,
  });
  final String label;
  final VoidCallback? onPressed;
  final IconData? icon;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 6),
    child: SizedBox(
      width: double.infinity,
      child: FilledButton.icon(
        onPressed: onPressed,
        icon: Icon(icon ?? Icons.arrow_forward),
        label: Text(label, style: const TextStyle(fontSize: 18)),
        style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(56)),
      ),
    ),
  );
}

Future<void> showError(BuildContext context, Object e) async {
  if (!context.mounted) return;
  ScaffoldMessenger.of(context)
      .showSnackBar(SnackBar(content: Text(e.toString())));
}
