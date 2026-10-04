import 'package:flutter/material.dart';

import '../api/models.dart';
import '../services.dart';
import '../util/time_format.dart';
import '../widgets/common.dart';

class AlertsScreen extends StatefulWidget {
  const AlertsScreen({
    super.key,
    required this.profileId,
    required this.wearerTz,
  });
  final String profileId;
  final String wearerTz;

  @override
  State<AlertsScreen> createState() => _AlertsScreenState();
}

class _AlertsScreenState extends State<AlertsScreen> {
  late Future<List<AlertItem>> _alerts;

  @override
  void initState() {
    super.initState();
    _alerts = services.api.alerts(widget.profileId);
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Alerts')),
    body: FutureBuilder<List<AlertItem>>(
      future: _alerts,
      builder: (context, snap) {
        if (snap.hasError) return Center(child: Text('${snap.error}'));
        if (!snap.hasData) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snap.data!.isEmpty) {
          return const Center(
            child: Text('No configured anomalies detected in available data'),
          );
        }
        return ListView(
          padding: const EdgeInsets.all(12),
          children: [
            for (final a in snap.data!)
              Card(
                color: a.status == 'resolved' ? null : Colors.orange.shade50,
                child: ListTile(
                  contentPadding: const EdgeInsets.all(12),
                  title: Text(
                    a.reason,
                    maxLines: 3,
                    overflow: TextOverflow.ellipsis,
                  ),
                  subtitle: Text(
                    '${a.status.toUpperCase()} · ${formatDual(a.openedAt, widget.wearerTz)}',
                  ),
                  trailing: a.isSimulated
                      ? const SourceBadge(simulation: true)
                      : null,
                  onTap: () async {
                    await Navigator.of(context).push(
                      MaterialPageRoute(
                        builder: (_) => AlertDetailScreen(alertId: a.id),
                      ),
                    );
                    setState(
                      () => _alerts = services.api.alerts(widget.profileId),
                    );
                  },
                ),
              ),
          ],
        );
      },
    ),
  );
}

/// Detail is fetched only after authentication (push payloads carry no vitals).
class AlertDetailScreen extends StatefulWidget {
  const AlertDetailScreen({super.key, required this.alertId});
  final String alertId;

  @override
  State<AlertDetailScreen> createState() => _AlertDetailScreenState();
}

class _AlertDetailScreenState extends State<AlertDetailScreen> {
  late Future<AlertItem> _alert;

  @override
  void initState() {
    super.initState();
    _alert = services.api.alert(widget.alertId);
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Alert')),
    body: FutureBuilder<AlertItem>(
      future: _alert,
      builder: (context, snap) {
        if (snap.hasError) {
          return Center(
            child: Text('This alert is not available to you. (${snap.error})'),
          );
        }
        if (!snap.hasData) {
          return const Center(child: CircularProgressIndicator());
        }
        final a = snap.data!;
        final tz = a.wearerTimezone;
        final n = a.notifications ?? const {};
        return ListView(
          padding: const EdgeInsets.all(16),
          children: [
            if (a.isSimulated)
              const SimulationBanner(text: 'SIMULATED ALERT - synthetic data'),
            Text(a.reason, style: const TextStyle(fontSize: 20)),
            const SizedBox(height: 12),
            Text(
              'Status: ${a.status.toUpperCase()}',
              style: const TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
            ),
            Text('Opened ${formatDual(a.openedAt, tz)}'),
            if (a.acknowledgedAt != null)
              Text('Acknowledged ${formatDual(a.acknowledgedAt!, tz)}'),
            if (a.resolvedAt != null)
              Text(
                'Resolved ${formatDual(a.resolvedAt!, tz)} (${a.resolutionReason})',
              ),
            const SizedBox(height: 8),
            Text(
              'Notifications accepted by the push provider: ${n['accepted_by_provider'] ?? 0} · '
              'queued: ${n['queued'] ?? 0}. Provider acceptance does not prove a phone showed it.',
            ),
            if (a.evidence != null) ...[
              const Divider(),
              const Text('Evidence', style: TextStyle(fontSize: 18)),
              for (final e in a.evidence!.entries) Text('${e.key}: ${e.value}'),
            ],
            const SizedBox(height: 12),
            if (a.status == 'open')
              BigButton(
                label: "I've seen this",
                icon: Icons.done,
                onPressed: () async {
                  try {
                    await services.api.acknowledge(a.id);
                    setState(() => _alert = services.api.alert(widget.alertId));
                  } catch (e) {
                    if (context.mounted) await showError(context, e);
                  }
                },
              ),
            const Text(
              'Acknowledging records that you saw the alert. It does not mean the '
              'situation has ended; the alert resolves only when new data shows recovery.',
            ),
          ],
        );
      },
    ),
  );
}
