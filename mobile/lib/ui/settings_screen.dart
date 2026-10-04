import 'package:flutter/material.dart';

import '../config.dart';
import '../services.dart';
import '../widgets/common.dart';

/// Settings + notification diagnostics: shows what is real (FCM) and what is not (fake
/// transport / push not configured) so nobody mistakes a dev setup for a working alert path.
class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  Map<String, dynamic>? _server;
  String? _testResult;

  @override
  void initState() {
    super.initState();
    services.api.diagnostics().then(
      (d) => mounted ? setState(() => _server = d) : null,
      onError: (Object e) =>
          mounted ? setState(() => _server = {'error': e.toString()}) : null,
    );
  }

  @override
  Widget build(BuildContext context) {
    final push = services.push;
    return Scaffold(
      appBar: AppBar(title: const Text('Settings & diagnostics')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text(
            'Signed in as ${services.auth.user?.email ?? services.auth.user?.id ?? '-'}',
            style: const TextStyle(fontSize: 18),
          ),
          Text('Server: ${AppConfig.apiBaseUrl}'),
          Text(
            'Auth mode: ${AppConfig.isDevAuth ? 'DEVELOPMENT (locally minted tokens)' : 'Supabase'}',
          ),
          const Divider(),
          const Text('Notifications', style: TextStyle(fontSize: 20)),
          Text(
            'This device: ${push.state}${push.permission != null ? ' · permission ${push.permission}' : ''}',
          ),
          if (push.lastError != null)
            Text(
              'Last error: ${push.lastError}',
              style: const TextStyle(color: Colors.red),
            ),
          Text(
            'Server push transport: ${_server?['push_transport_label'] ?? _server?['error'] ?? '…'}',
            key: const Key('server-push-transport'),
          ),
          BigButton(
            label: 'Register for notifications',
            icon: Icons.notifications_active,
            onPressed: () async {
              await push.enable(await services.installationId());
              setState(() {});
            },
          ),
          BigButton(
            label: 'Send test notification',
            icon: Icons.send,
            onPressed: push.registeredDevice == null
                ? null
                : () async {
                    try {
                      final r = await push.sendTest();
                      setState(
                        () => _testResult =
                            '${r['transport']}: ${r['provider_status']}. ${r['note']}',
                      );
                    } catch (e) {
                      setState(() => _testResult = e.toString());
                    }
                  },
          ),
          if (_testResult != null) Text(_testResult!),
          const Divider(),
          if (_server != null) ...[
            const Text('Server', style: TextStyle(fontSize: 20)),
            for (final e in _server!.entries) Text('${e.key}: ${e.value}'),
          ],
          const Divider(),
          OutlinedButton(
            onPressed: () async {
              await services.auth.signOut();
              if (context.mounted) {
                Navigator.of(context).popUntil((r) => r.isFirst);
              }
            },
            child: const Padding(
              padding: EdgeInsets.all(12),
              child: Text('Sign out'),
            ),
          ),
          const Text(
            'Signing out on the wearer phone stops collection and deletes queued, '
            'not-yet-uploaded readings from this phone.',
          ),
          const SizedBox(height: 24),
          TextButton(
            style: TextButton.styleFrom(foregroundColor: Colors.red),
            onPressed: () async {
              final ok = await showDialog<bool>(
                context: context,
                builder: (c) => AlertDialog(
                  title: const Text('Delete my FamilyPulse account data?'),
                  content: const Text(
                    'Deletes your shared readings, alerts, sharing permissions and devices '
                    'from the server and signs you out. Your sign-in identity is removed '
                    'separately by the administrator.',
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
                await services.api.deleteMyAccountData();
                await services.auth.signOut();
                if (context.mounted) {
                  Navigator.of(context).popUntil((r) => r.isFirst);
                }
              } catch (e) {
                if (context.mounted) await showError(context, e);
              }
            },
            child: const Text('Delete my account data'),
          ),
        ],
      ),
    );
  }
}
