import 'package:flutter/material.dart';

import 'collector/collector_bridge.dart';
import 'services.dart';
import 'ui/alert_screens.dart';
import 'ui/home_screen.dart';
import 'ui/sign_in_screen.dart';
import 'ui/wearer_screens.dart';
import 'util/time_format.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  initTimezones();
  services = Services.create();
  await services.auth.restore();
  await services.push.init();
  String? launchAction;
  if (CollectorBridge.isSupported) {
    try {
      launchAction = await CollectorBridge.launchAction();
    } catch (_) {}
  }
  runApp(
    FamilyPulseApp(
      showPrivacyFirst:
          launchAction != null && launchAction != 'android.intent.action.MAIN',
    ),
  );
}

ThemeData buildTheme() => ThemeData(
  colorSchemeSeed: Colors.teal,
  useMaterial3: true,
  cardTheme: const CardThemeData(margin: EdgeInsets.symmetric(vertical: 6)),
);

/// Larger text by default for readability, while still honouring bigger system settings.
Widget largeText(BuildContext context, Widget? child) =>
    MediaQuery.withClampedTextScaling(
      minScaleFactor: 1.15,
      child: child ?? const SizedBox(),
    );

class FamilyPulseApp extends StatefulWidget {
  const FamilyPulseApp({super.key, this.showPrivacyFirst = false});
  final bool showPrivacyFirst;

  @override
  State<FamilyPulseApp> createState() => _FamilyPulseAppState();
}

class _FamilyPulseAppState extends State<FamilyPulseApp> {
  @override
  void initState() {
    super.initState();
    services.auth.addListener(_onAuth);
    // Deep link from a push notification: open the alert after authenticating.
    services.push.alertTaps.listen((alertId) {
      services.navigatorKey.currentState?.push(
        MaterialPageRoute(builder: (_) => AlertDetailScreen(alertId: alertId)),
      );
    });
  }

  void _onAuth() => setState(() {});

  @override
  void dispose() {
    services.auth.removeListener(_onAuth);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'FamilyPulse',
    navigatorKey: services.navigatorKey,
    theme: buildTheme(),
    builder: largeText,
    home: widget.showPrivacyFirst
        ? const PrivacyScreen()
        : services.auth.signedIn
        ? const HomeScreen()
        : const SignInScreen(),
  );
}
