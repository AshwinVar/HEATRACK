import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api/models.dart';
import '../collector/collector_bridge.dart';
import '../config.dart';
import '../services.dart';
import '../sim/simulator.dart';
import '../util/time_format.dart';
import '../widgets/common.dart';
import 'caregiver_screens.dart';

const consentPoints = [
  'You choose which family members can see your readings, and what they can see.',
  'A family member only gets access after you confirm who they are.',
  'You can stop sharing with anyone at any time; it takes effect immediately.',
  'FamilyPulse only READS heart rate, resting heart rate, steps and sleep from Health Connect. '
      'It never writes to Health Connect.',
  'FamilyPulse is not an emergency service and does not diagnose anything. '
      'Alerts may be late or missed if the watch or phone does not sync.',
  'You can turn collection off or delete all your shared data from this app.',
];

/// Shown from Health Connect's "privacy policy / why this app needs access" links.
class PrivacyScreen extends StatelessWidget {
  const PrivacyScreen({super.key});

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('How FamilyPulse uses your data')),
    body: ListView(
      padding: const EdgeInsets.all(20),
      children: [
        for (final p in consentPoints)
          ListTile(
            leading: const Icon(Icons.check),
            title: Text(p, style: const TextStyle(fontSize: 18)),
          ),
      ],
    ),
  );
}

class OnboardingScreen extends StatefulWidget {
  const OnboardingScreen({super.key, required this.simulation});
  final bool simulation;

  @override
  State<OnboardingScreen> createState() => _OnboardingScreenState();
}

class _OnboardingScreenState extends State<OnboardingScreen> {
  final _name = TextEditingController();
  final _phone = TextEditingController();
  bool _agreed = false;
  bool _busy = false;

  Future<void> _create() async {
    setState(() => _busy = true);
    try {
      final p = await services.api.createHealthProfile(
        displayName: _name.text.trim().isEmpty ? 'Me' : _name.text.trim(),
        dataMode: widget.simulation ? 'simulation' : 'live',
        phone: _phone.text.trim(),
      );
      if (!mounted) return;
      await Navigator.of(context).pushReplacement(
        MaterialPageRoute(builder: (_) => WearerHomeScreen(profile: p)),
      );
    } catch (e) {
      if (mounted) await showError(context, e);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final sim = widget.simulation;
    return Scaffold(
      appBar: AppBar(
        title: Text(sim ? 'Demo setup (simulated)' : 'Share my readings'),
      ),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          if (sim)
            const SimulationBanner(
              text: 'SIMULATION: this profile only ever contains synthetic, made-up data.',
            ),
          if (!sim && !CollectorBridge.isSupported)
            const Card(
              child: Padding(
                padding: EdgeInsets.all(12),
                child: Text(
                  'Reading watch data needs the FamilyPulse Android app on the phone that '
                  'syncs the watch (Health Connect is Android-only). You can still create '
                  'the profile here.',
                  style: TextStyle(fontSize: 16),
                ),
              ),
            ),
          const SizedBox(height: 8),
          Text(
            'Before you start',
            style: Theme.of(context).textTheme.headlineSmall,
          ),
          for (final p in consentPoints)
            ListTile(
              leading: const Icon(Icons.info_outline),
              title: Text(p, style: const TextStyle(fontSize: 17)),
            ),
          TextField(
            controller: _name,
            decoration: const InputDecoration(
              labelText: 'Name your family will see',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _phone,
            keyboardType: TextInputType.phone,
            decoration: const InputDecoration(
              labelText: 'Phone number family can call (optional)',
              border: OutlineInputBorder(),
            ),
          ),
          CheckboxListTile(
            value: _agreed,
            onChanged: (v) => setState(() => _agreed = v ?? false),
            title: const Text(
              'I understand and want to continue',
              style: TextStyle(fontSize: 18),
            ),
          ),
          BigButton(
            label: sim ? 'Create demo profile' : 'Continue',
            onPressed: _agreed && !_busy ? _create : null,
          ),
        ],
      ),
    );
  }
}

class WearerHomeScreen extends StatefulWidget {
  const WearerHomeScreen({super.key, required this.profile});
  final HealthProfileInfo profile;

  @override
  State<WearerHomeScreen> createState() => _WearerHomeScreenState();
}

class _WearerHomeScreenState extends State<WearerHomeScreen> {
  Map<String, dynamic>? _status;
  String? _message;
  bool _busy = false;

  HealthProfileInfo get p => widget.profile;
  bool get _native => CollectorBridge.isSupported && !p.isSimulation;

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  Future<void> _refresh() async {
    if (!_native) return;
    try {
      final s = await CollectorBridge.status();
      if (mounted) setState(() => _status = s);
    } catch (e) {
      if (mounted) {
        setState(() => _message = 'Collector status unavailable: $e');
      }
    }
  }

  Future<void> _run(String label, Future<String?> Function() f) async {
    setState(() {
      _busy = true;
      _message = '$label…';
    });
    try {
      final m = await f();
      if (mounted) setState(() => _message = m ?? '$label: done');
    } catch (e) {
      if (mounted) setState(() => _message = '$label failed: $e');
    } finally {
      if (mounted) setState(() => _busy = false);
      await _refresh();
    }
  }

  Future<String?> _setUpCollector() async {
    final hc = await CollectorBridge.healthConnectStatus();
    if (hc != 'available') {
      await CollectorBridge.openHealthConnect();
      return 'Health Connect is $hc. Install or update it, then come back and tap this again.';
    }
    final granted = await CollectorBridge.requestPermissions(
      includeBackground: true,
    );
    final status = await CollectorBridge.status();
    final device = await services.api.registerCollector(
      p.id,
      await services.installationId(),
      (status['capabilities'] as Map).cast<String, dynamic>(),
    );
    await CollectorBridge.configure(
      apiBaseUrl: AppConfig.apiBaseUrl,
      profileId: p.id,
      deviceId: device['id'] as String,
    );
    await CollectorBridge.enable();
    final sync = await CollectorBridge.syncNow();
    return 'Permissions granted: ${granted.length}. First sync: ${sync['status']} '
        '(${sync['error_category'] ?? 'no errors'}).';
  }

  Future<String?> _syncNow() async {
    final s = await CollectorBridge.syncNow();
    final found = (s['metrics_found'] as Map?)?.entries
        .map((e) => '${e.key}: ${e.value}')
        .join(', ');
    return 'Sync ${s['status']}${s['error_category'] != null ? ' (${s['error_category']})' : ''}. '
        'New samples: ${found == null || found.isEmpty ? 'none' : found}. '
        'Waiting to upload: ${s['queue_size']}.';
  }

  Future<String?> _disable() async {
    await CollectorBridge.disable();
    final id = _status?['device_id'] as String?;
    if (id != null) await services.api.disableCollector(id);
    return 'Collection turned off. Queued data on this phone was deleted.';
  }

  Future<void> _delete() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Delete all shared data?'),
        content: const Text(
          'This deletes every reading, alert and sharing permission for this profile on '
          'the server, stops collection and removes queued data from this phone. '
          'It cannot be undone.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(c, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(c, true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    try {
      if (_native) await CollectorBridge.disable();
      await services.api.deleteHealthProfile(p.id);
      if (mounted) Navigator.of(context).pop();
    } catch (e) {
      if (mounted) await showError(context, e);
    }
  }

  Widget _collectorCard() {
    final s = _status;
    if (!_native) return const SizedBox.shrink();
    final caps = (s?['capabilities'] as Map?) ?? {};
    final bg = caps['background_read'];
    final enabled = s?['enabled'] == true;
    final last = s?['last_sync'] as Map?;
    final foregroundOnly = bg != 'granted';
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Watch data collection',
              style: Theme.of(context).textTheme.titleLarge,
            ),
            const SizedBox(height: 8),
            Text(
              'Health Connect: ${caps['health_connect'] ?? 'unknown'}',
              style: const TextStyle(fontSize: 17),
            ),
            Text(
              'Collection: ${enabled ? 'ON' : 'OFF'}',
              style: const TextStyle(fontSize: 17),
            ),
            if (enabled && foregroundOnly)
              Container(
                key: const Key('foreground-only'),
                margin: const EdgeInsets.only(top: 8),
                padding: const EdgeInsets.all(8),
                color: Colors.orange.shade100,
                child: const Text(
                  'FOREGROUND ONLY: background reading is not permitted or not supported on this '
                  'phone. Readings upload only when you open the app and tap "Sync now".',
                  style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold),
                ),
              ),
            if (enabled && !foregroundOnly)
              const Text(
                'Background sync is on. Android runs it roughly every 15 minutes or later '
                '(battery saving can delay it by hours). The watch must also sync to the Fitbit app.',
                style: TextStyle(fontSize: 15),
              ),
            if (last != null)
              Text(
                'Last sync: ${last['status']} (${last['trigger']}) '
                '${last['ended_at'] != null ? formatDual(DateTime.parse(last['ended_at'] as String), p.timezone) : ''}',
              ),
            Text(
              'Waiting to upload: ${s?['queue_size'] ?? 0} batch(es)'
              '${(s?['dropped_batches'] ?? 0) != 0 ? ' · dropped: ${s!['dropped_batches']}' : ''}',
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final enabled = _status?['enabled'] == true;
    return Scaffold(
      appBar: AppBar(
        title: Text(p.isSimulation ? 'Demo profile' : 'My sharing'),
        actions: [
          Padding(
            padding: const EdgeInsets.all(8),
            child: SourceBadge(simulation: p.isSimulation),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _refresh,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            if (p.isSimulation)
              const SimulationBanner(
                text: 'SIMULATED DATA - this profile never contains real readings',
              ),
            if (_message != null)
              Card(
                color: Colors.blueGrey.shade50,
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Text(_message!, style: const TextStyle(fontSize: 16)),
                ),
              ),
            _collectorCard(),
            if (_native && !enabled)
              BigButton(
                label: 'Connect watch data (Health Connect)',
                icon: Icons.watch,
                onPressed: _busy
                    ? null
                    : () => _run('Setting up', _setUpCollector),
              ),
            if (_native && enabled) ...[
              BigButton(
                label: 'Sync now',
                icon: Icons.sync,
                onPressed: _busy ? null : () => _run('Syncing', _syncNow),
              ),
              BigButton(
                label: 'Health Connect permissions',
                icon: Icons.lock_open,
                onPressed: _busy
                    ? null
                    : () => _run('Permissions', () async {
                        final g = await CollectorBridge.requestPermissions(
                          includeBackground: true,
                        );
                        return 'Granted: ${g.length} permission(s)';
                      }),
              ),
            ],
            if (!CollectorBridge.isSupported && !p.isSimulation)
              const Card(
                child: Padding(
                  padding: EdgeInsets.all(12),
                  child: Text(
                    'Watch data can only be collected by the Android app.',
                  ),
                ),
              ),
            if (p.isSimulation) ...[
              BigButton(
                label: 'Send normal SIMULATED readings',
                icon: Icons.science,
                onPressed: _busy
                    ? null
                    : () => _run('Simulating', () async {
                        final r = await Simulator(
                          services.api,
                        ).pushHeartRate(p.id, await services.installationId());
                        return 'Uploaded ${r['samples_written']} synthetic samples';
                      }),
              ),
              BigButton(
                label: 'Send ELEVATED SIMULATED readings',
                icon: Icons.science,
                onPressed: _busy
                    ? null
                    : () => _run('Simulating', () async {
                        final r = await Simulator(services.api).pushHeartRate(
                          p.id,
                          await services.installationId(),
                          elevated: true,
                        );
                        return 'Uploaded ${r['samples_written']} synthetic elevated samples. '
                            'An alert appears within ~1 minute if the synthetic demo rule is on.';
                      }),
              ),
            ],
            BigButton(
              label: "I'm OK (check in)",
              icon: Icons.thumb_up,
              onPressed: _busy
                  ? null
                  : () => _run('Check-in', () async {
                      await services.api.checkIn(p.id);
                      return 'Check-in sent (a time stamp only, not a health reading).';
                    }),
            ),
            BigButton(
              label: 'Who can see my readings',
              icon: Icons.people,
              onPressed: () => Navigator.of(context).push(
                MaterialPageRoute(builder: (_) => SharingScreen(profile: p)),
              ),
            ),
            BigButton(
              label: 'View my dashboard',
              icon: Icons.dashboard,
              onPressed: () => Navigator.of(context).push(
                MaterialPageRoute(
                  builder: (_) => DashboardScreen(profileId: p.id),
                ),
              ),
            ),
            OutlinedButton(
              onPressed: () => Navigator.of(context).push(
                MaterialPageRoute(builder: (_) => RulesScreen(profile: p)),
              ),
              child: const Padding(
                padding: EdgeInsets.all(12),
                child: Text('Alert rules'),
              ),
            ),
            if (_native)
              OutlinedButton(
                onPressed: () => Navigator.of(context).push(
                  MaterialPageRoute(
                    builder: (_) => SourceDiagnosticsScreen(profile: p),
                  ),
                ),
                child: const Padding(
                  padding: EdgeInsets.all(12),
                  child: Text('Source diagnostics'),
                ),
              ),
            if (_native && enabled)
              OutlinedButton(
                onPressed: _busy ? null : () => _run('Turning off', _disable),
                child: const Padding(
                  padding: EdgeInsets.all(12),
                  child: Text('Turn off collection'),
                ),
              ),
            const SizedBox(height: 16),
            TextButton(
              onPressed: _delete,
              style: TextButton.styleFrom(foregroundColor: Colors.red),
              child: const Text(
                'Delete all my shared data',
                style: TextStyle(fontSize: 16),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// The integration spike as a screen: what Health Connect actually holds on this phone.
class SourceDiagnosticsScreen extends StatefulWidget {
  const SourceDiagnosticsScreen({super.key, required this.profile});
  final HealthProfileInfo profile;

  @override
  State<SourceDiagnosticsScreen> createState() =>
      _SourceDiagnosticsScreenState();
}

class _SourceDiagnosticsScreenState extends State<SourceDiagnosticsScreen> {
  Map<String, dynamic>? _diag;
  Map<String, dynamic>? _status;
  List<Map<String, dynamic>>? _runs;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final st = await CollectorBridge.status();
      final d = await CollectorBridge.diagnose(hours: 24);
      final runs = await services.api.syncRuns(widget.profile.id);
      if (mounted) {
        setState(() {
          _status = st;
          _diag = d;
          _runs = runs;
        });
      }
    } catch (e) {
      if (mounted) setState(() => _error = e.toString());
    }
  }

  @override
  Widget build(BuildContext context) {
    const enc = JsonEncoder.withIndent('  ');
    final metrics = (_diag?['metrics'] as Map?) ?? {};
    return Scaffold(
      appBar: AppBar(title: const Text('Source diagnostics')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          const Text(
            'Reads Health Connect on this phone (nothing is uploaded) to show which apps write '
            'data, how often, and how late. Export support does not prove your watch writes '
            'records: this screen shows what is actually there.',
            style: TextStyle(fontSize: 16),
          ),
          if (_error != null)
            Text('Error: $_error', style: const TextStyle(color: Colors.red)),
          if (_diag == null && _error == null)
            const Center(child: CircularProgressIndicator()),
          if (_diag != null) ...[
            Text(
              'Health Connect: ${_diag!['health_connect']}',
              style: const TextStyle(fontSize: 18),
            ),
            Text(
              'Background reading feature: ${_diag!['background_feature']} · '
              'permission: ${_diag!['background_permission']}',
            ),
            for (final e in metrics.entries)
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        metricLabels[e.key] ?? e.key.toString(),
                        style: Theme.of(context).textTheme.titleMedium,
                      ),
                      for (final kv in (e.value as Map).entries)
                        Text('${kv.key}: ${kv.value}'),
                    ],
                  ),
                ),
              ),
          ],
          if (_status != null) ...[
            const Divider(),
            const Text('Collector state', style: TextStyle(fontSize: 18)),
            SelectableText(enc.convert(_status)),
          ],
          if (_runs != null) ...[
            const Divider(),
            const Text(
              'Recent sync runs reported to the server',
              style: TextStyle(fontSize: 18),
            ),
            for (final r in _runs!.take(15))
              Text(
                '${r['started_at']} · ${r['trigger']} · ${r['status']}'
                '${r['error_category'] != null ? ' · ${r['error_category']}' : ''} · ${r['metrics_found']}',
              ),
          ],
          BigButton(label: 'Run again', icon: Icons.refresh, onPressed: _load),
          BigButton(
            label: 'Open Health Connect',
            icon: Icons.open_in_new,
            onPressed: () => CollectorBridge.openHealthConnect(),
          ),
        ],
      ),
    );
  }
}

class SharingScreen extends StatefulWidget {
  const SharingScreen({super.key, required this.profile});
  final HealthProfileInfo profile;

  @override
  State<SharingScreen> createState() => _SharingScreenState();
}

class _SharingScreenState extends State<SharingScreen> {
  static const _allCategories = {
    'heart': 'Heart rate',
    'sleep': 'Sleep',
    'activity': 'Steps',
    'alerts': 'Alerts',
    'connectivity': 'Phone check-ins',
  };
  final _selected = _allCategories.keys.toSet();
  Map<String, dynamic>? _invite;
  late Future<List<Grant>> _grants;

  @override
  void initState() {
    super.initState();
    _grants = services.api.grants(widget.profile.id);
  }

  void _reload() =>
      setState(() => _grants = services.api.grants(widget.profile.id));

  Future<void> _act(Future<void> Function() f) async {
    try {
      await f();
    } catch (e) {
      if (mounted) await showError(context, e);
    }
    _reload();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Who can see my readings')),
    body: ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Text(
          'Invite a family member',
          style: Theme.of(context).textTheme.titleLarge,
        ),
        for (final c in _allCategories.entries)
          CheckboxListTile(
            value: _selected.contains(c.key),
            title: Text(c.value, style: const TextStyle(fontSize: 18)),
            onChanged: (v) => setState(
              () => v == true ? _selected.add(c.key) : _selected.remove(c.key),
            ),
          ),
        BigButton(
          label: 'Create invitation code',
          icon: Icons.qr_code,
          onPressed: _selected.isEmpty
              ? null
              : () => _act(() async {
                  final inv = await services.api.createInvitation(
                    widget.profile.id,
                    _selected.toList(),
                  );
                  setState(() => _invite = inv);
                }),
        ),
        if (_invite != null)
          Card(
            color: Colors.teal.shade50,
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                children: [
                  const Text(
                    'Give this code to your family member (it works once, for 30 minutes):',
                  ),
                  SelectableText(
                    _invite!['token'] as String,
                    style: const TextStyle(
                      fontSize: 22,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                  TextButton.icon(
                    onPressed: () => Clipboard.setData(
                      ClipboardData(text: _invite!['token'] as String),
                    ),
                    icon: const Icon(Icons.copy),
                    label: const Text('Copy'),
                  ),
                ],
              ),
            ),
          ),
        const Divider(height: 32),
        FutureBuilder<List<Grant>>(
          future: _grants,
          builder: (context, snap) {
            if (!snap.hasData) {
              return const Center(child: CircularProgressIndicator());
            }
            final grants = snap.data!
                .where((g) => g.status != 'revoked')
                .toList();
            if (grants.isEmpty) {
              return const Text('Nobody can see your readings yet.');
            }
            return Column(
              children: [
                for (final g in grants)
                  Card(
                    child: Padding(
                      padding: const EdgeInsets.all(12),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            '${g.caregiverName?.isNotEmpty == true ? g.caregiverName : 'Unnamed'} '
                            '(${g.caregiverEmail ?? 'no email'})',
                            style: const TextStyle(
                              fontSize: 18,
                              fontWeight: FontWeight.bold,
                            ),
                          ),
                          Text('Can see: ${g.categories.join(', ')}'),
                          Text(
                            'Status: ${g.status == 'pending' ? 'WAITING FOR YOUR CONFIRMATION' : 'Active'}',
                          ),
                          if (g.status == 'pending') ...[
                            const Text(
                              'Only confirm if you recognise this person and email.',
                            ),
                            BigButton(
                              label: 'Yes, this is my family member',
                              icon: Icons.verified_user,
                              onPressed: () =>
                                  _act(() => services.api.confirmGrant(g.id)),
                            ),
                          ],
                          OutlinedButton(
                            onPressed: () =>
                                _act(() => services.api.revokeGrant(g.id)),
                            child: Text(
                              g.status == 'pending'
                                  ? 'Decline'
                                  : 'Stop sharing with this person',
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
              ],
            );
          },
        ),
      ],
    ),
  );
}

/// Owner rule settings. Health thresholds stay off until configured and explicitly
/// reviewed for the wearer; simulation profiles may only use synthetic demo rules.
class RulesScreen extends StatefulWidget {
  const RulesScreen({super.key, required this.profile});
  final HealthProfileInfo profile;

  @override
  State<RulesScreen> createState() => _RulesScreenState();
}

class _RulesScreenState extends State<RulesScreen> {
  late Future<List<Map<String, dynamic>>> _rules;

  @override
  void initState() {
    super.initState();
    _rules = services.api.rules(widget.profile.id);
  }

  Future<void> _edit(Map<String, dynamic> r) async {
    final sim = widget.profile.isSimulation;
    final thr = TextEditingController(text: '${r['threshold_value'] ?? ''}');
    final rec = TextEditingController(text: '${r['recovery_value'] ?? ''}');
    var enabled = r['enabled'] as bool;
    var reviewed = false;
    final isThreshold = r['kind'] == 'threshold';
    await showDialog<void>(
      context: context,
      builder: (c) => StatefulBuilder(
        builder: (c, set) => AlertDialog(
          title: Text(
            '${r['kind']} ${r['metric'] ?? ''} ${r['direction'] ?? ''}',
          ),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                SwitchListTile(
                  value: enabled,
                  onChanged: (v) => set(() => enabled = v),
                  title: const Text('Enabled'),
                ),
                if (isThreshold) ...[
                  TextField(
                    controller: thr,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(labelText: 'Limit (bpm)'),
                  ),
                  TextField(
                    controller: rec,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(
                      labelText: 'Recovery value (hysteresis)',
                    ),
                  ),
                  if (sim)
                    const Text(
                      'SYNTHETIC DEMO threshold: not medical advice.',
                      style: TextStyle(color: Colors.deepPurple),
                    )
                  else
                    CheckboxListTile(
                      value: reviewed,
                      onChanged: (v) => set(() => reviewed = v ?? false),
                      title: const Text(
                        'These values were reviewed for this wearer (e.g. with her doctor). '
                        'FamilyPulse does not provide medical thresholds.',
                      ),
                    ),
                ],
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(c),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () async {
                try {
                  await services.api.updateRule(
                    widget.profile.id,
                    r['id'] as String,
                    {
                      'enabled': enabled,
                      if (isThreshold && thr.text.isNotEmpty)
                        'threshold_value': double.parse(thr.text),
                      if (isThreshold && rec.text.isNotEmpty)
                        'recovery_value': double.parse(rec.text),
                      if (isThreshold && sim) 'is_synthetic_demo': true,
                      if (isThreshold && !sim) 'reviewed_for_wearer': reviewed,
                    },
                  );
                  if (c.mounted) Navigator.pop(c);
                } catch (e) {
                  if (c.mounted) await showError(c, e);
                }
              },
              child: const Text('Save'),
            ),
          ],
        ),
      ),
    );
    setState(() => _rules = services.api.rules(widget.profile.id));
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Alert rules')),
    body: FutureBuilder<List<Map<String, dynamic>>>(
      future: _rules,
      builder: (context, snap) {
        if (!snap.hasData) {
          return const Center(child: CircularProgressIndicator());
        }
        return ListView(
          padding: const EdgeInsets.all(16),
          children: [
            const Text(
              'Health limits are off until you set and confirm them. Engineering '
              'checks (phone not checking in, no new data) are on by default.',
            ),
            for (final r in snap.data!)
              Card(
                child: ListTile(
                  title: Text(
                    '${r['kind']} · ${r['metric'] ?? 'phone'} ${r['direction'] ?? ''}',
                  ),
                  subtitle: Text(
                    '${r['enabled'] == true ? 'ON' : 'off'}'
                    '${r['threshold_value'] != null ? ' · limit ${r['threshold_value']}' : ''}'
                    '${r['is_synthetic_demo'] == true ? ' · SYNTHETIC DEMO' : ''}'
                    ' · v${r['config_version']}',
                  ),
                  trailing: widget.profile.isOwner
                      ? const Icon(Icons.edit)
                      : null,
                  onTap: widget.profile.isOwner ? () => _edit(r) : null,
                ),
              ),
          ],
        );
      },
    ),
  );
}
