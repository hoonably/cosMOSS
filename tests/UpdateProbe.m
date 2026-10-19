// Exercise the shipped Sparkle framework against a signed local test feed.
// This probe only discovers updates; it never installs or touches the user's app.
#import <AppKit/AppKit.h>
#import <Sparkle/Sparkle.h>

@interface UpdateProbe : NSObject <NSApplicationDelegate, SPUUpdaterDelegate>
@property(nonatomic, strong) SPUStandardUpdaterController *controller;
@end

@implementation UpdateProbe
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    self.controller = [[SPUStandardUpdaterController alloc] initWithStartingUpdater:NO updaterDelegate:self userDriverDelegate:nil];
    [self.controller startUpdater];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC / 2), dispatch_get_main_queue(), ^{
        [self.controller.updater checkForUpdateInformation];
    });
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 20 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        fprintf(stderr, "Update probe timed out\n"); exit(3);
    });
}
- (void)updater:(SPUUpdater *)updater didFindValidUpdate:(SUAppcastItem *)item {
    if (getenv("EXPECT_REJECTION")) { fprintf(stderr, "Accepted a tampered feed\n"); exit(2); }
    printf("FOUND:%s:%s\n", item.displayVersionString.UTF8String, item.versionString.UTF8String);
    exit(0);
}
- (void)updater:(SPUUpdater *)updater didFinishUpdateCycleForUpdateCheck:(SPUUpdateCheck)updateCheck error:(NSError *)error {
    if (getenv("EXPECT_REJECTION") && error != nil) {
        printf("REJECTED:%s\n", error.description.UTF8String); exit(0);
    }
    fprintf(stderr, "Update cycle ended without finding candidate: %s\n", error.description.UTF8String); exit(2);
}
@end

int main(void) {
    @autoreleasepool {
        NSApplication *app = NSApplication.sharedApplication;
        [app setActivationPolicy:NSApplicationActivationPolicyProhibited];
        UpdateProbe *probe = [UpdateProbe new];
        app.delegate = probe;
        [app run];
    }
    return 0;
}
