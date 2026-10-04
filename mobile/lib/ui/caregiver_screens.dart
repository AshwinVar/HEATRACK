import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../api/models.dart';
import '../services.dart';
import '../util/time_format.dart';
import '../widgets/common.dart';
import '../widgets/hr_chart.dart';
import 'alert_screens.dart';

class AcceptInviteScreen extends StatefulWidget {
  const AcceptInviteScreen({super.key});

  @override
  State<AcceptInviteScreen> createState() => _AcceptInviteScreenState();
}

class _AcceptInviteScreenState extends State<AcceptInviteScreen> {
  final _code = TextEditingController();
  String? _result;

  Future<void> _accept() async {
    try {
      final g = await services.api.acceptInvitation(_code.text.trim());
      setState(
        () => _result =
            'Request sent to ${g.wearerName ?? 'the wearer'}. '
            'You will see their readings after they confirm it is you.',
      );
    } catch (e) {
      if (mounted) await showError(context, e);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Invitation code')),
    body: ListView(
      padding: const EdgeInsets.all(20),
      children: [
        TextField(
          controller: _code,
          decoration: const InputDecoration(
            labelText: 'Code',
            border: OutlineInputBorder(),
          ),
        ),
        BigButton(label: 'Send request', icon: Icons.send, onPressed: _accept),
        if (_result != null)
          Text(_result!, style: const TextStyle(fontSize: 18)),
      ],
    ),
  );
}

class DashboardScreen extends StatefulWidget {
  const DashboardScreen({super.key, required this.profileId});
  final String profileId;

  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  late Future<(Dashboard, List<MeasurementPoint>)> _data;
  late DateTime _to;

  @override
  void initState() {
    super.initState();
    _load();
  }

  void _load() {
    _to = DateTime.now().toUtc();
    final from = _to.subtract(const Duration(hours: 24));
    _data = () async {
      final d = await services.api.dashboard(widget.profileId);
      final hr = d.metric('heart_rate') == null
          ? <MeasurementPoint>[]
          : await services.api.measurements(
              widget.profileId,
              'heart_rate',
              from,
              _to,
            );
      return (d, hr);
    }();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Dashboard')),
    body: RefreshIndicator(
      onRefresh: () async => setState(_load),
      child: FutureBuilder<(Dashboard, List<MeasurementPoint>)>(
        future: _data,
        builder: (context, snap) {
          if (snap.hasError) {
            return ListView(
              children: [
                Padding(
                  padding: const EdgeInsets.all(20),
                  child: Text('Could not load: ${snap.error}'),
                ),
              ],
            );
          }
          if (!snap.hasData) {
            return const Center(child: CircularProgressIndicator());
          }
          final (d, hr) = snap.data!;
          return DashboardView(
            dashboard: d,
            heartRate: hr,
            chartFrom: _to.subtract(const Duration(hours: 24)),
            chartTo: _to,
            onOpenAlerts: () => Navigator.of(context).push(
              MaterialPageRoute(
                builder: (_) => AlertsScreen(
                  profileId: widget.profileId,
                  wearerTz: d.profile.timezone,
                ),
              ),
            ),
            onCall: d.profile.contactPhone == null
                ? null
                : () => launchUrl(
                    Uri(scheme: 'tel', path: d.profile.contactPhone),
                  ),
          );
        },
      ),
    ),
  );
}

/// Pure rendering of a dashboard payload (widget-tested with fixtures).
class DashboardView extends StatelessWidget {
  const DashboardView({
    super.key,
    required this.dashboard,
    required this.heartRate,
    required this.chartFrom,
    required this.chartTo,
    this.onOpenAlerts,
    this.onCall,
  });

  final Dashboard dashboard;
  final List<MeasurementPoint> heartRate;
  final DateTime chartFrom;
  final DateTime chartTo;
  final VoidCallback? onOpenAlerts;
  final VoidCallback? onCall;

  @override
  Widget build(BuildContext context) {
    final d = dashboard;
    final tz = d.profile.timezone;
    final theme = Theme.of(context);
    final collector = d.collector;
    final devices =
        (collector?['devices'] as List?)?.cast<Map<String, dynamic>>() ??
        const [];
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        if (d.banner != null) SimulationBanner(text: d.banner!),
        Row(
          children: [
            Expanded(
              child: Text(
                d.profile.displayName,
                style: theme.textTheme.headlineMedium,
              ),
            ),
            SourceBadge(simulation: d.isSimulation),
          ],
        ),
        Text('Times shown in $tz (wearer) and this device\'s local time.'),
        const SizedBox(height: 8),
        Card(
          color: d.openAlerts.isNotEmpty ? Colors.orange.shade50 : null,
          child: ListTile(
            contentPadding: const EdgeInsets.all(12),
            leading: Icon(
              d.openAlerts.isNotEmpty
                  ? Icons.notification_important
                  : Icons.info_outline,
              size: 36,
            ),
            title: Text(
              d.summaryText,
              key: const Key('summary-text'),
              style: const TextStyle(fontSize: 20),
            ),
            subtitle: d.healthThresholdsConfigured
                ? null
                : const Text(
                    'No health limits have been configured for this person.',
                  ),
            trailing: const Icon(Icons.chevron_right),
            onTap: onOpenAlerts,
          ),
        ),
        if (onCall != null)
          BigButton(
            label: 'Call ${d.profile.displayName}',
            icon: Icons.call,
            onPressed: onCall,
          ),
        for (final m in d.metrics) MetricCard(status: m, wearerTz: tz),
        if (d.metric('heart_rate') != null) ...[
          const SizedBox(height: 8),
          Text('Heart rate, last 24 hours', style: theme.textTheme.titleLarge),
          HeartRateChart(points: heartRate, from: chartFrom, to: chartTo),
        ],
        if (d.daily.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text('Daily (days in $tz)', style: theme.textTheme.titleLarge),
          for (final day in d.daily.reversed) _DailyCard(day: day),
        ],
        for (final i in d.insights) _InsightCard(insight: i),
        if (collector != null) ...[
          const SizedBox(height: 8),
          Text('Phone connection', style: theme.textTheme.titleLarge),
          if (devices.isEmpty) const Text('No collector phone registered.'),
          for (final dev in devices)
            Card(
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Check-in: ${dev['heartbeat_state']}',
                      style: const TextStyle(fontSize: 18),
                    ),
                    if (dev['last_heartbeat_at'] != null)
                      Text(
                        'Last phone check-in ${formatDual(DateTime.parse(dev['last_heartbeat_at'] as String), tz)}',
                      ),
                    if (dev['last_upload_at'] != null)
                      Text(
                        'Last upload ${formatDual(DateTime.parse(dev['last_upload_at'] as String), tz)}',
                      ),
                    Text(
                      'Background sync: ${dev['background_sync'] ?? 'unknown'}',
                    ),
                    const Text(
                      'A phone check-in only shows the phone is reachable; it is not a health reading.',
                    ),
                  ],
                ),
              ),
            ),
          if (collector['last_check_in_at'] != null)
            Text(
              "Last 'I'm OK': ${formatDual(DateTime.parse(collector['last_check_in_at'] as String), tz)}",
              style: const TextStyle(fontSize: 17),
            ),
        ],
        const SizedBox(height: 8),
        ExpansionTile(
          title: const Text('Not available'),
          children: [
            for (final e in d.unavailable.entries)
              ListTile(title: Text(e.key), subtitle: Text(e.value)),
          ],
        ),
        const Padding(
          padding: EdgeInsets.symmetric(vertical: 12),
          child: Text(
            'FamilyPulse is a wellness-sharing prototype. It is not an emergency service or a '
            'medical device, and alerts can be delayed or missed.',
            style: TextStyle(fontStyle: FontStyle.italic),
          ),
        ),
      ],
    );
  }
}

class _DailyCard extends StatelessWidget {
  const _DailyCard({required this.day});
  final Map<String, dynamic> day;

  String _sel(Map? m, String metric) {
    if (m == null) return '–';
    final byOrigin = (m['by_origin'] as Map?) ?? {};
    if (byOrigin.isEmpty) return 'no data';
    final sel = m['selected'] as Map?;
    if (sel?['value'] != null) {
      return formatValue(metric, (sel!['value'] as num).toDouble());
    }
    // Several sources and no preferred one: show each, never a combined sum.
    return byOrigin.entries
        .map(
          (e) =>
              '${formatValue(metric, (e.value as num).toDouble())} (${e.key})',
        )
        .join(' / ');
  }

  @override
  Widget build(BuildContext context) => Card(
    child: ListTile(
      title: Text(day['date'] as String),
      subtitle: Text(
        [
          if (day.containsKey('steps'))
            'Steps: ${_sel(day['steps'] as Map?, 'steps')}'
                '${(day['steps'] as Map?)?['complete_day'] == false ? ' (so far today)' : ''}',
          if (day.containsKey('sleep_minutes'))
            'Sleep: ${_sel(day['sleep_minutes'] as Map?, 'sleep')}',
          if (day.containsKey('resting_heart_rate'))
            'Resting HR: ${_sel(day['resting_heart_rate'] as Map?, 'resting_heart_rate')}',
        ].join('\n'),
      ),
    ),
  );
}

class _InsightCard extends StatelessWidget {
  const _InsightCard({required this.insight});
  final Map<String, dynamic> insight;

  @override
  Widget build(BuildContext context) {
    final metric = metricLabels[insight['metric']] ?? insight['metric'];
    final state = insight['state'];
    final text = switch (state) {
      'insufficient_baseline' =>
        '$metric trend: not enough history yet (${insight['valid_prior_days']} of '
            '${insight['required_days']} days needed).',
      'no_target_value' =>
        '$metric trend: no value for ${insight['target_day']}.',
      _ =>
        '$metric on ${insight['target_day']}: ${insight['value']} vs usual '
            '${insight['baseline_median']} (robust deviation ${insight['robust_deviation']}). '
            '${insight['note'] ?? ''}',
    };
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Text(text, style: const TextStyle(fontSize: 16)),
      ),
    );
  }
}
