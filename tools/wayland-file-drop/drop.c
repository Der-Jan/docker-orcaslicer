#define _GNU_SOURCE
#include <wayland-client.h>
#include "layer-shell.h"
#include "virtual-pointer.h"
#include <errno.h>
#include <fcntl.h>
#include <linux/input-event-codes.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static struct wl_display *display;
static struct wl_compositor *compositor;
static struct wl_shm *shm;
static struct wl_seat *seat;
static struct wl_pointer *pointer;
static struct wl_data_device_manager *data_manager;
static struct wl_data_device *device;
static struct wl_data_source *source;
static struct wl_surface *surface;
static struct zwlr_layer_shell_v1 *layer_shell;
static struct zwlr_layer_surface_v1 *layer;
static struct zwlr_virtual_pointer_manager_v1 *virtual_manager;
static struct zwlr_virtual_pointer_v1 *virtual_pointer;
static struct wl_buffer *buffer;
static char *uris;
static bool mapped, entered, started, finished, cancelled, pressed, sent;
static unsigned width=1024, height=768, target_x=512, target_y=384;
static unsigned output_count;
static bool have_mode;
static volatile sig_atomic_t interrupted;

static void interrupt_drag(int sig) { (void)sig; interrupted=1; }
static void output_geometry(void *d,struct wl_output *o,int32_t x,int32_t y,int32_t pw,int32_t ph,int32_t sub,const char *make,const char *model,int32_t transform) {
    (void)d; (void)o; (void)x; (void)y; (void)pw; (void)ph; (void)sub; (void)make; (void)model; (void)transform;
}
static void output_mode(void *d,struct wl_output *o,uint32_t flags,int32_t w,int32_t h,int32_t refresh) {
    (void)d; (void)o; (void)refresh;
    if (flags & WL_OUTPUT_MODE_CURRENT) { width=(unsigned)w; height=(unsigned)h; have_mode=true; }
}
static const struct wl_output_listener output_listener={.geometry=output_geometry,.mode=output_mode};

static uint32_t now_ms(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint32_t)(t.tv_sec * 1000 + t.tv_nsec / 1000000);
}
static void move_to(unsigned x, unsigned y) {
    zwlr_virtual_pointer_v1_motion_absolute(virtual_pointer, now_ms(), x, y, width, height);
    zwlr_virtual_pointer_v1_frame(virtual_pointer);
}
static void button(uint32_t state) {
    zwlr_virtual_pointer_v1_button(virtual_pointer, now_ms(), BTN_LEFT, state);
    zwlr_virtual_pointer_v1_frame(virtual_pointer);
    pressed = state == WL_POINTER_BUTTON_STATE_PRESSED;
}
static bool pump_until(bool *condition, unsigned timeout) {
    uint32_t end=now_ms()+timeout;
    while (!*condition && !cancelled && !interrupted && (int32_t)(end-now_ms()) > 0) {
        while (wl_display_prepare_read(display) != 0)
            if (wl_display_dispatch_pending(display) < 0) return false;
        if (wl_display_flush(display) < 0 && errno != EAGAIN) {
            wl_display_cancel_read(display); return false;
        }
        struct pollfd p={wl_display_get_fd(display), POLLIN, 0};
        int ready=poll(&p,1,50);
        if (ready>0 && (p.revents & POLLIN)) {
            if (wl_display_read_events(display)<0) return false;
        } else wl_display_cancel_read(display);
        if (wl_display_dispatch_pending(display)<0) return false;
    }
    return *condition;
}
static void settle(unsigned milliseconds) {
    bool never=false;
    pump_until(&never,milliseconds);
}
static void data_target(void *d, struct wl_data_source *s, const char *mime) {
    (void)d; (void)s; fprintf(stderr,"Target accepts: %s\n", mime ? mime : "none");
}
static void data_send(void *d, struct wl_data_source *s, const char *mime, int32_t fd) {
    (void)d; (void)s;
    if (strcmp(mime,"text/uri-list")) { close(fd); return; }
    size_t offset=0, size=strlen(uris);
    while (offset<size) {
        ssize_t n=write(fd,uris+offset,size-offset);
        if (n<0 && errno==EINTR) continue;
        if (n<=0) break;
        offset+=(size_t)n;
    }
    sent=offset==size;
    close(fd);
    fprintf(stderr,"URI transfer: %s\n",sent ? "complete" : "failed");
}
static void data_cancel(void *d, struct wl_data_source *s) {
    (void)d; (void)s;
    if (!finished) { cancelled=true; fprintf(stderr,"Drag cancelled\n"); }
}
static void data_drop(void *d, struct wl_data_source *s) {
    (void)d; (void)s; fprintf(stderr,"Drop performed\n");
}
static void data_finish(void *d, struct wl_data_source *s) {
    (void)d; (void)s; finished=true; fprintf(stderr,"Destination finished drag\n");
}
static void data_action(void *d, struct wl_data_source *s, uint32_t action) {
    (void)d; (void)s; fprintf(stderr,"Drag action: %u\n",action);
}
static const struct wl_data_source_listener source_listener={
    .target=data_target,.send=data_send,.cancelled=data_cancel,
    .dnd_drop_performed=data_drop,.dnd_finished=data_finish,.action=data_action
};
static void pointer_enter(void *d,struct wl_pointer *p,uint32_t serial,
                          struct wl_surface *s,wl_fixed_t x,wl_fixed_t y) {
    (void)d; (void)p; (void)serial; (void)x; (void)y;
    entered=s==surface;
    if (entered) fprintf(stderr,"Pointer entered source surface\n");
}
static void pointer_leave(void *d,struct wl_pointer *p,uint32_t serial,struct wl_surface *s) {
    (void)d; (void)p; (void)serial; (void)s; entered=false;
}
static void pointer_motion(void *d,struct wl_pointer *p,uint32_t t,wl_fixed_t x,wl_fixed_t y) {
    (void)d; (void)p; (void)t; (void)x; (void)y;
}
static void pointer_button(void *d,struct wl_pointer *p,uint32_t serial,uint32_t t,uint32_t b,uint32_t state) {
    (void)d; (void)p; (void)t;
    if (entered && !started && b==BTN_LEFT && state==WL_POINTER_BUTTON_STATE_PRESSED) {
        wl_data_device_start_drag(device,source,surface,NULL,serial);
        started=true;
        fprintf(stderr,"Started native drag with pointer serial %u\n",serial);
    }
}
static void pointer_axis(void *d,struct wl_pointer *p,uint32_t t,uint32_t a,wl_fixed_t v) {
    (void)d; (void)p; (void)t; (void)a; (void)v;
}
static const struct wl_pointer_listener pointer_listener={
    .enter=pointer_enter,.leave=pointer_leave,.motion=pointer_motion,
    .button=pointer_button,.axis=pointer_axis
};
static void seat_caps(void *d,struct wl_seat *s,uint32_t caps) {
    (void)d;
    if ((caps & WL_SEAT_CAPABILITY_POINTER) && !pointer) {
        pointer=wl_seat_get_pointer(s);
        wl_pointer_add_listener(pointer,&pointer_listener,NULL);
    }
}
static const struct wl_seat_listener seat_listener={.capabilities=seat_caps};
static void configure(void *d,struct zwlr_layer_surface_v1 *s,uint32_t serial,uint32_t w,uint32_t h) {
    (void)d; (void)w; (void)h;
    zwlr_layer_surface_v1_ack_configure(s,serial);
    wl_surface_attach(surface,buffer,0,0);
    wl_surface_damage(surface,0,0,80,80);
    wl_surface_commit(surface);
    mapped=true;
}
static void layer_closed(void *d,struct zwlr_layer_surface_v1 *s) {
    (void)d; (void)s; cancelled=true;
}
static const struct zwlr_layer_surface_v1_listener layer_listener={.configure=configure,.closed=layer_closed};
static void global(void *d,struct wl_registry *r,uint32_t name,const char *interface,uint32_t version) {
    (void)d; (void)version;
    if (!strcmp(interface,"wl_output")) {
        output_count++;
        if (output_count==1) {
            struct wl_output *output=wl_registry_bind(r,name,&wl_output_interface,1);
            wl_output_add_listener(output,&output_listener,NULL);
        }
    } else if (!strcmp(interface,"wl_compositor")) compositor=wl_registry_bind(r,name,&wl_compositor_interface,1);
    else if (!strcmp(interface,"wl_shm")) shm=wl_registry_bind(r,name,&wl_shm_interface,1);
    else if (!strcmp(interface,"wl_seat") && !seat) {
        seat=wl_registry_bind(r,name,&wl_seat_interface,1);
        wl_seat_add_listener(seat,&seat_listener,NULL);
    } else if (!strcmp(interface,"wl_data_device_manager") && version>=3)
        data_manager=wl_registry_bind(r,name,&wl_data_device_manager_interface,3);
    else if (!strcmp(interface,"zwlr_layer_shell_v1"))
        layer_shell=wl_registry_bind(r,name,&zwlr_layer_shell_v1_interface,1);
    else if (!strcmp(interface,"zwlr_virtual_pointer_manager_v1"))
        virtual_manager=wl_registry_bind(r,name,&zwlr_virtual_pointer_manager_v1_interface,1);
}
static void global_remove(void *d,struct wl_registry *r,uint32_t name) { (void)d; (void)r; (void)name; }
static const struct wl_registry_listener registry_listener={global,global_remove};

int main(int argc,char **argv) {
    if (argc!=2 && argc!=6) {
        fprintf(stderr,"Usage: %s FILE [OUTPUT_WIDTH OUTPUT_HEIGHT DROP_X DROP_Y]\n       %s --inspect\n",argv[0],argv[0]); return 2;
    }
    bool inspect=argc==2 && !strcmp(argv[1],"--inspect");
    unsigned explicit_width=width, explicit_height=height;
    if (argc==6) {
        unsigned *values[]={&width,&height,&target_x,&target_y};
        for (int i=0;i<4;i++) {
            char *end; unsigned long v=strtoul(argv[i+2],&end,10);
            if (!*argv[i+2] || *end || v>100000) return 2;
            *values[i]=(unsigned)v;
        }
        if (width<100 || height<100 || target_x>=width || target_y>=height || (target_x<80 && target_y<80)) return 2;
        explicit_width=width; explicit_height=height;
    }
    char *path=inspect ? strdup("/dev/null") : realpath(argv[1],NULL);
    if (!path || access(path,R_OK)) { perror("File"); free(path); return 2; }
    struct stat st;
    if (!inspect && (stat(path,&st)<0 || !S_ISREG(st.st_mode))) {
        fprintf(stderr,"File must be a readable regular file\n"); free(path); return 2;
    }
    uris=calloc(strlen(path)*3+16,1);
    if (!uris) return 2;
    strcpy(uris,"file://");
    size_t n=strlen(uris);
    for (const unsigned char *p=(unsigned char *)path;*p;p++) {
        if ((*p>='a' && *p<='z') || (*p>='A' && *p<='Z') || (*p>='0' && *p<='9') || strchr("/-._~",*p)) uris[n++]=*p;
        else { snprintf(uris+n,4,"%%%02X",*p); n+=3; }
    }
    strcpy(uris+n,"\r\n"); free(path);
    signal(SIGPIPE,SIG_IGN);
    signal(SIGTERM,interrupt_drag);
    signal(SIGINT,interrupt_drag);
    display=wl_display_connect(NULL);
    if (!display) { perror("Wayland connection"); return 2; }
    struct wl_registry *registry=wl_display_get_registry(display);
    wl_registry_add_listener(registry,&registry_listener,NULL);
    wl_display_roundtrip(display); wl_display_roundtrip(display);
    if (argc==2) {
        if (output_count!=1 || !have_mode || width<100 || height<100) {
            fprintf(stderr,"Automatic center drop requires exactly one active output\n"); return 2;
        }
        target_x=width/2; target_y=height/2;
    } else { width=explicit_width; height=explicit_height; }
    fprintf(stderr,"Drop position: %u,%u on %ux%u\n",target_x,target_y,width,height);
    if (!compositor || !shm || !seat || !pointer || !data_manager || !layer_shell || !virtual_manager) {
        fprintf(stderr,"Required compositor interfaces unavailable\n"); return 2;
    }
    if (inspect) { wl_display_disconnect(display); free(uris); return 0; }
    int fd=memfd_create("dnd-source",MFD_CLOEXEC);
    size_t size=80*80*4;
    if (fd<0 || ftruncate(fd,(off_t)size)<0) { perror("Source buffer"); return 2; }
    uint32_t *pixels=mmap(NULL,size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);
    if (pixels==MAP_FAILED) return 2;
    for (unsigned i=0;i<80*80;i++) pixels[i]=0xff22aa88;
    struct wl_shm_pool *pool=wl_shm_create_pool(shm,fd,(int)size);
    buffer=wl_shm_pool_create_buffer(pool,0,80,80,80*4,WL_SHM_FORMAT_ARGB8888);
    wl_shm_pool_destroy(pool); close(fd);
    surface=wl_compositor_create_surface(compositor);
    layer=zwlr_layer_shell_v1_get_layer_surface(layer_shell,surface,NULL,ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY,"dnd-prototype");
    zwlr_layer_surface_v1_set_size(layer,80,80);
    zwlr_layer_surface_v1_set_anchor(layer,ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP|ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT);
    zwlr_layer_surface_v1_set_exclusive_zone(layer,-1);
    zwlr_layer_surface_v1_add_listener(layer,&layer_listener,NULL);
    wl_surface_commit(surface);
    device=wl_data_device_manager_get_data_device(data_manager,seat);
    source=wl_data_device_manager_create_data_source(data_manager);
    wl_data_source_add_listener(source,&source_listener,NULL);
    wl_data_source_offer(source,"text/uri-list");
    wl_data_source_set_actions(source,WL_DATA_DEVICE_MANAGER_DND_ACTION_COPY);
    virtual_pointer=zwlr_virtual_pointer_manager_v1_create_virtual_pointer(virtual_manager,seat);
    bool success=false;
    if (!pump_until(&mapped,3000)) goto cleanup;
    settle(200);
    move_to(40,40);
    if (!pump_until(&entered,3000)) goto cleanup;
    button(WL_POINTER_BUTTON_STATE_PRESSED);
    if (!pump_until(&started,3000)) goto cleanup;
    settle(150);
    move_to(target_x,target_y);
    settle(500);
    button(WL_POINTER_BUTTON_STATE_RELEASED);
    success=pump_until(&finished,15000) && sent;
cleanup:
    if (pressed) button(WL_POINTER_BUTTON_STATE_RELEASED);
    wl_data_source_destroy(source);
    zwlr_layer_surface_v1_destroy(layer);
    wl_surface_destroy(surface);
    zwlr_virtual_pointer_v1_destroy(virtual_pointer);
    wl_display_flush(display);
    wl_display_disconnect(display);
    munmap(pixels,size); free(uris);
    fprintf(stderr,"Result: %s\n",success ? "native file drop completed" : "drop not confirmed (do not automatically retry)");
    return success ? 0 : 1;
}
