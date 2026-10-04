import 'package:flutter/material.dart';

import '../api/models.dart';
import '../collector/collector_bridge.dart';
import '../services.dart';
import '../widgets/common.dart';
import 'caregiver_screens.dart';
import 'settings_screen.dart';
import 'wearer_screens.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  late Future<List<HealthProfileInfo>> _profiles;

  @override
  void initState() {
    super.initState();
    _profiles = services.api.healthProfiles();
    _enablePush();
  }

  Future<void> _enablePush() async {
    try {
      await services.push.enable(await services.installationId());
    } catch (_) {
      // Diagnostics screen shows push state; the app works without push.
    }
  }

  void _reload() => setState(() => _profiles = services.api.healthProfiles());

  Future<void> _open(Widget w) async {
    await Navigator.of(context).push(MaterialPageRoute(builder: (_) => w));
    _reload();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      title: const Text('FamilyPulse'),
      actions: [
        IconButton(
          tooltip: 'Settings and diagnostics',
          icon: const Icon(Icons.settings),
          onPressed: () => _open(const SettingsScreen()),
        ),
      ],
    ),
    body: RefreshIndicator(
      onRefresh: () async => _reload(),
      child: FutureBuilder<List<HealthProfileInfo>>(
        future: _profiles,
        builder: (context, snap) {
          if (snap.hasError) {
            return ListView(
              padding: const EdgeInsets.all(20),
              children: [
                Text('Could not reach the server: ${snap.error}'),
                BigButton(
                  label: 'Retry',
                  icon: Icons.refresh,
                  onPressed: _reload,
                ),
              ],
            );
          }
          if (!snap.hasData) {
            return const Center(child: CircularProgressIndicator());
          }
          final all = snap.data!;
          final ownLive = all
              .where((p) => p.isOwner && !p.isSimulation)
              .firstOrNull;
          final ownSim = all
              .where((p) => p.isOwner && p.isSimulation)
              .firstOrNull;
          final caring = all.where((p) => !p.isOwner).toList();
          return ListView(
            padding: const EdgeInsets.all(16),
            children: [
              Text(
                'People I care for',
                style: Theme.of(context).textTheme.headlineSmall,
              ),
              if (caring.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 8),
                  child: Text('No one has shared with you yet.'),
                ),
              for (final p in caring)
                Card(
                  child: ListTile(
                    contentPadding: const EdgeInsets.all(12),
                    title: Text(
                      p.displayName,
                      style: const TextStyle(fontSize: 20),
                    ),
                    subtitle: Text('Shared: ${p.categories.join(', ')}'),
                    trailing: SourceBadge(simulation: p.isSimulation),
                    onTap: () => _open(DashboardScreen(profileId: p.id)),
                  ),
                ),
              BigButton(
                label: 'Enter an invitation code',
                icon: Icons.group_add,
                onPressed: () => _open(const AcceptInviteScreen()),
              ),
              const Divider(height: 32),
              Text(
                'Sharing my own readings',
                style: Theme.of(context).textTheme.headlineSmall,
              ),
              if (ownLive != null)
                Card(
                  child: ListTile(
                    contentPadding: const EdgeInsets.all(12),
                    title: const Text(
                      'My sharing',
                      style: TextStyle(fontSize: 20),
                    ),
                    subtitle: const Text(
                      'Watch data, permissions, Sync now, who can see it',
                    ),
                    trailing: const SourceBadge(simulation: false),
                    onTap: () => _open(WearerHomeScreen(profile: ownLive)),
                  ),
                )
              else
                BigButton(
                  label: CollectorBridge.isSupported
                      ? 'Set up sharing from this phone'
                      : 'Set up sharing (requires the Android app)',
                  icon: Icons.favorite,
                  onPressed: () =>
                      _open(const OnboardingScreen(simulation: false)),
                ),
              const Divider(height: 32),
              Text('Demo', style: Theme.of(context).textTheme.headlineSmall),
              if (ownSim != null)
                Card(
                  color: Colors.deepPurple.shade50,
                  child: ListTile(
                    contentPadding: const EdgeInsets.all(12),
                    title: const Text('Demo profile (SIMULATED data)'),
                    trailing: const SourceBadge(simulation: true),
                    onTap: () => _open(WearerHomeScreen(profile: ownSim)),
                  ),
                )
              else
                OutlinedButton(
                  onPressed: () =>
                      _open(const OnboardingScreen(simulation: true)),
                  child: const Padding(
                    padding: EdgeInsets.all(12),
                    child: Text(
                      'Try a demo with simulated data',
                      style: TextStyle(fontSize: 18),
                    ),
                  ),
                ),
            ],
          );
        },
      ),
    ),
  );
}
