import AVFoundation
import UIKit

@main
final class AppDelegate: UIResponder, UIApplicationDelegate {

    var window: UIWindow?

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        configureAudioSession()
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.backgroundColor = .black
        window.rootViewController = FeedViewController()
        window.makeKeyAndVisible()
        self.window = window
        return true
    }

    func applicationDidBecomeActive(_ application: UIApplication) {
        // 被电话/其他 App 打断后回来，音频会话可能被降级，重新激活
        configureAudioSession()
    }

    /// 播放类别：静音拨片拨到静音也照样出声（和抖音一致），网页里的视频才有声音。
    private func configureAudioSession() {
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.playback, mode: .moviePlayback)
            try session.setActive(true)
        } catch {
            NSLog("DyMon audio session error: \(error)")
        }
    }
}
